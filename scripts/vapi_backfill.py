"""
vapi_backfill.py — Vapi API 経由で過去 call の transcript を raw に backfill
                  (★2026-05-23 海山指示)

背景:
  5/21-23 に海山が複数回 Vapi で通話したが、Vapi クレジット切れで Assistant 起動失敗
  → end-of-call-report の transcript が空 or 未配信
  → main.py /webhook/voice-alignment が silent skip
  → data/brain/raw/alignment_voice/ に save されず

クレジット補充後、Vapi API は過去 call の transcript / artifact をまだ保持してる。
本 script で API 直叩きして raw に流し込む → 既存 extract_session に乗せて蒸留される。

使い方:
  # 5/20 以降の全 call を backfill (= 重複は skip)
  python3 scripts/vapi_backfill.py --since 2026-05-20

  # 特定 call ID 1 件だけ
  python3 scripts/vapi_backfill.py --call-id 019e_xxx

  # dry-run (= API call はするが file write しない)
  python3 scripts/vapi_backfill.py --since 2026-05-20 --dry-run

env:
  VAPI_PRIVATE_API_KEY — Vapi private API key (https://dashboard.vapi.ai → API Keys)

動作:
  1. Vapi GET /api/call?createdAtGt=<since> で call list 取得
  2. 各 call の artifact.transcript / messages を取り出し
  3. 80 字以上の有意 transcript なら data/brain/raw/alignment_voice/YYYY-MM-DD-HHMM.md に保存
  4. 既存 file あれば skip (= 重複防止)
  5. 保存件数を report

蒸留 (LLM 蒸留 → wiki/interview):
  本 script は raw 保存のみ。蒸留は **海山が手動で trigger** する想定:

  ```bash
  docker exec line-bot python3 -c "
  import asyncio, sys
  sys.path.insert(0, '/app')
  import alignment_interview as ai
  import httpx, os
  async def main():
      http = httpx.AsyncClient(timeout=60.0)
      for raw_file in sorted((ai.RAW_DIR).glob('2026-05-2*.md')):
          if (ai.EXTRACTED_DIR / (raw_file.stem + '.json')).exists():
              continue  # 既蒸留 skip
          transcript = raw_file.read_text(encoding='utf-8')
          await ai.extract_session(transcript, http, os.getenv('LITELLM_URL'),
              os.getenv('LITELLM_MASTER_KEY'), raw_filename=raw_file.name, model='smart')
          print(f'extracted: {raw_file.name}')
      await http.aclose()
  asyncio.run(main())
  "
  ```
  あるいは LW で `/align-voice` 走らせれば最新の蒸留候補が見える。
"""
from __future__ import annotations

import argparse
import json
import logging
import os
import re
import sys
from datetime import datetime, timezone, timedelta
from pathlib import Path

import httpx

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger("vapi_backfill")

JST = timezone(timedelta(hours=9))
APP_ROOT = Path(os.getenv("BRAIN_APP_ROOT", "/app"))
RAW_DIR = APP_ROOT / "data" / "brain" / "raw" / "alignment_voice"

VAPI_API_BASE = os.getenv("VAPI_API_BASE", "https://api.vapi.ai")
VAPI_API_KEY = os.getenv("VAPI_PRIVATE_API_KEY", "")

# 80 字未満は ノイズ (= 無音通話 / 失敗)、main.py の skip 閾値と揃える
MIN_TRANSCRIPT_CHARS = int(os.getenv("VAPI_BACKFILL_MIN_CHARS", "80"))


def list_calls_since(since: str, limit: int = 100) -> list[dict]:
    """Vapi GET /api/call?createdAtGt=<ISO> で call list 取得。

    Vapi API 仕様: https://docs.vapi.ai/api-reference/calls/list
    """
    url = f"{VAPI_API_BASE}/call"
    params = {"createdAtGt": since, "limit": str(limit)}
    headers = {"Authorization": f"Bearer {VAPI_API_KEY}"}
    with httpx.Client(timeout=30.0) as client:
        r = client.get(url, params=params, headers=headers)
        r.raise_for_status()
        return r.json()


def fetch_call(call_id: str, api_key: str = "") -> dict:
    """Vapi GET /api/call/{id} で詳細 (artifact + transcript) 取得。

    Args:
        call_id: Vapi call UUID
        api_key: override 用 (= ★2026-05-23 海山指示 MacBook 完結、body 経由で渡される時用)
                 空なら env VAPI_PRIVATE_API_KEY を使う
    """
    url = f"{VAPI_API_BASE}/call/{call_id}"
    key = (api_key or VAPI_API_KEY).strip()
    headers = {"Authorization": f"Bearer {key}"}
    with httpx.Client(timeout=30.0) as client:
        r = client.get(url, headers=headers)
        r.raise_for_status()
        return r.json()


def extract_transcript(call: dict) -> str:
    """call dict から transcript を抽出 (main.py の voice_alignment_webhook と同じ揺れ対応)。"""
    artifact = call.get("artifact") or {}

    # 整形済 transcript string
    t = (artifact.get("transcript") or "").strip()
    if t:
        return t

    # messages array → role + content 整形
    def _from_msgs(arr):
        out = []
        for m in arr or []:
            role = (m.get("role") or "").lower()
            if role == "system":
                continue
            content = m.get("message") or m.get("content") or m.get("text") or ""
            if isinstance(content, list):
                content = " ".join(c.get("text", "") for c in content if isinstance(c, dict))
            content = (content or "").strip()
            if not content:
                continue
            who = "海山" if role in ("user", "customer") else "AI"
            out.append(f"{who}: {content}")
        return "\n".join(out)

    return (
        _from_msgs(artifact.get("messages"))
        or _from_msgs(artifact.get("messagesOpenAIFormatted"))
        or (call.get("transcript") or "").strip()
    )


def save_raw(call: dict, transcript: str, dry_run: bool = False) -> Path | None:
    """raw/alignment_voice/YYYY-MM-DD-HHMM.md として保存。重複なら skip。

    file 名は main.py /webhook/voice-alignment が record_session で使う形式に揃える。
    """
    # createdAt から JST 時刻取得
    created = call.get("createdAt") or call.get("startedAt") or ""
    try:
        # ISO with Z → datetime
        dt = datetime.fromisoformat(created.replace("Z", "+00:00")).astimezone(JST)
    except Exception:
        dt = datetime.now(JST)
    ts = dt.strftime("%Y-%m-%d-%H%M")
    raw_path = RAW_DIR / f"{ts}.md"

    if raw_path.exists():
        logger.info(f"skip (exists): {raw_path.name}")
        return None

    header = (
        f"---\n"
        f"type: alignment_interview\n"
        f"source: vapi_backfill\n"
        f"vapi_call_id: {call.get('id', '?')}\n"
        f"recorded_at: {dt.isoformat(timespec='seconds')}\n"
        f"duration_sec: {call.get('duration', '?')}\n"
        f"ended_reason: {call.get('endedReason', '?')}\n"
        f"---\n\n"
    )
    body = header + transcript

    if dry_run:
        logger.info(f"[dry-run] would save {raw_path.name} ({len(transcript)} chars)")
        return raw_path
    RAW_DIR.mkdir(parents=True, exist_ok=True)
    raw_path.write_text(body, encoding="utf-8")
    logger.info(f"saved {raw_path.name} ({len(transcript)} chars)")
    return raw_path


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--since", default="", help="ISO date (例 2026-05-20)、空なら過去 7 日")
    parser.add_argument("--call-id", default="", help="特定 call ID 1 件のみ")
    parser.add_argument("--dry-run", action="store_true", help="API 叩くが file 書かない")
    parser.add_argument("--min-chars", type=int, default=MIN_TRANSCRIPT_CHARS,
                        help=f"transcript の最低字数 (default {MIN_TRANSCRIPT_CHARS})")
    args = parser.parse_args()

    if not VAPI_API_KEY:
        logger.error("VAPI_PRIVATE_API_KEY が .env に未設定 (Vapi dashboard → API Keys から取得)")
        return 1

    # 単一 call mode
    if args.call_id:
        try:
            call = fetch_call(args.call_id)
        except Exception as e:
            logger.error(f"fetch failed: {e}")
            return 1
        transcript = extract_transcript(call)
        if not transcript or len(transcript) < args.min_chars:
            logger.info(f"skip (transcript {len(transcript)} chars < {args.min_chars}): {args.call_id}")
            return 0
        save_raw(call, transcript, dry_run=args.dry_run)
        return 0

    # range mode
    since = args.since or (datetime.now(JST) - timedelta(days=7)).strftime("%Y-%m-%d")
    # ISO format に整形
    if re.match(r"^\d{4}-\d{2}-\d{2}$", since):
        since = f"{since}T00:00:00.000Z"

    logger.info(f"listing Vapi calls since {since}")
    try:
        calls = list_calls_since(since)
    except Exception as e:
        logger.error(f"list failed: {e}")
        return 1
    logger.info(f"fetched {len(calls)} calls")

    n_saved = 0
    n_skipped_short = 0
    n_skipped_exists = 0
    n_error = 0

    for call in calls:
        cid = call.get("id", "?")
        ended = call.get("endedReason", "?")
        try:
            # list API では transcript / artifact 取れない (= summary だけ)、call 単位で fetch
            detail = fetch_call(cid)
            transcript = extract_transcript(detail)
            if not transcript or len(transcript) < args.min_chars:
                n_skipped_short += 1
                logger.info(f"  [{cid[:8]}] skip short ({len(transcript)} chars, ended={ended})")
                continue
            result = save_raw(detail, transcript, dry_run=args.dry_run)
            if result is None:
                n_skipped_exists += 1
            else:
                n_saved += 1
        except Exception as e:
            n_error += 1
            logger.warning(f"  [{cid[:8]}] error: {e}")

    print()
    print(f"=== backfill summary ===")
    print(f"calls listed:       {len(calls)}")
    print(f"saved (new raw):    {n_saved}")
    print(f"skip (already raw): {n_skipped_exists}")
    print(f"skip (transcript <{args.min_chars} chars): {n_skipped_short}")
    print(f"error:              {n_error}")
    print()
    if n_saved > 0 and not args.dry_run:
        print("次のステップ: 蒸留を走らせるなら docker exec で alignment_interview.extract_session を回す")
        print("(docstring 参照)、or 海山 LW で /align-voice 確認")

    return 0


if __name__ == "__main__":
    sys.exit(main())
