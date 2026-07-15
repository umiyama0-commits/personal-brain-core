"""services/conversation_success.py — 「会話が続いた turn」 = positive signal 蓄積

★2026-05-26 海山指示「会話が続いたデータをある種の正解として system 改善に反映させる loop」:
bot 応答後、user が follow-up メッセージを送った turn = 「応答 OK だった」 推定。
これを正解 dataset として蓄積、style / prompt 改善の base にする。

検出 logic:
  1. user message 受信時に直前 bot turn (= clone_history.assistant) を確認
  2. 「直前 bot 応答から N 分以内」 + 「修正でない (= _looks_like_correction = False)」 で continuation
  3. record_success に append

negative signal (= audit bad/fix, clone_feedback, data_gap) は既に蓄積中 (= 既存 module 群)。
本 module は positive signal の対応物、改善 cycle を「失敗だけでなく成功も学ぶ」 方向へ拡張。

保存先: data/brain/clone_review/conversation_success.jsonl (= append-only)

record schema:
{
  "id": "cont_<12 hex>",
  "timestamp": "2026-05-26T...",
  "user_id": "<truncated 16 chars>",
  "channel_id": "<dm or channel_xxx>",
  "user_query": "<元 query、bot response の trigger>",
  "bot_response": "<bot の応答全文>",
  "continuation": "<user の次 message>",
  "elapsed_seconds": 234,
  "status": "captured|reviewed|applied|skipped"
}
"""
from __future__ import annotations

import json
import logging
import os
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path

from services._review_store import (
    append_jsonl,
    locked,
    read_jsonl,
    write_jsonl_atomic,
)

logger = logging.getLogger(__name__)

JST = timezone(timedelta(hours=9))
BRAIN_ROOT = Path(os.getenv("BRAIN_ROOT", "/app/data/brain"))
SUCCESS_DIR = BRAIN_ROOT / "clone_review"
SUCCESS_FILE = SUCCESS_DIR / "conversation_success.jsonl"

# 「会話継続」 とみなす最大経過時間 (= これより長いと「別話題で再開」 扱い、skip)
MAX_CONTINUATION_SECONDS = int(os.getenv("CONTINUATION_MAX_SEC", str(30 * 60)))  # default 30 min

# 修正 keyword (= 簡易、_looks_like_correction の重複 lite 版、main.py の関数が import 不可な時 fallback)
_CORRECTION_KEYWORDS = (
    "違う", "違います", "ちがう", "正しくは", "間違", "そうじゃない",
    "それじゃない", "じゃなくて", "じゃなく", "NG", "no,",
)


def _ensure_dir() -> None:
    SUCCESS_DIR.mkdir(parents=True, exist_ok=True)


def _now_iso() -> str:
    return datetime.now(JST).isoformat(timespec="seconds")


def _is_correction(text: str) -> bool:
    """簡易修正検出 (= 厳密版は main.py の _looks_like_correction を使うこと)."""
    t = (text or "").strip().lower()
    if not t:
        return False
    return any(kw in t for kw in _CORRECTION_KEYWORDS)


def record_success(
    user_id: str,
    channel_id: str | None,
    user_query: str,
    bot_response: str,
    continuation: str,
    elapsed_seconds: float | None = None,
) -> str:
    """継続 turn を記録. 返り値: 採番 id."""
    _ensure_dir()
    rec = {
        "id": f"cont_{uuid.uuid4().hex[:12]}",
        "timestamp": _now_iso(),
        "user_id": (user_id or "")[:16],
        "channel_id": (channel_id or "dm")[:20] if channel_id else "dm",
        "user_query": (user_query or "")[:500],
        "bot_response": (bot_response or "")[:800],
        "continuation": (continuation or "")[:500],
        "elapsed_seconds": elapsed_seconds,
        "status": "captured",
    }
    with locked(SUCCESS_FILE):
        append_jsonl(SUCCESS_FILE, rec)
    logger.info(f"conversation_success captured: {rec['id']} (elapsed={elapsed_seconds}s)")
    return rec["id"]


def detect_continuation(
    user_id: str,
    current_text: str,
    history: list[dict],
    channel_id: str | None = None,
    max_seconds: int = MAX_CONTINUATION_SECONDS,
) -> dict | None:
    """history (= clone_history.load_recent 直近 N 件) から 継続 turn を検出.

    Returns: continuation 検出時、record_success に渡すべき dict; 無ければ None
    {
        "user_query": str,
        "bot_response": str,
        "continuation": str,
        "elapsed_seconds": float | None,
    }
    """
    if not current_text or not history:
        return None
    # 修正なら skip (= positive 信号にならない)
    if _is_correction(current_text):
        return None
    # 直前 turn が assistant かつ その前が user か検出
    last_assistant = None
    last_assistant_ts = None
    last_user_query = None
    # history は古い → 新しい順
    n = len(history)
    for i in range(n - 1, -1, -1):
        turn = history[i]
        if turn.get("role") == "assistant" and last_assistant is None:
            last_assistant = turn.get("content", "")
            last_assistant_ts = turn.get("timestamp", "")
            # その前の user turn を探す
            for j in range(i - 1, -1, -1):
                prev = history[j]
                if prev.get("role") == "user":
                    last_user_query = prev.get("content", "")
                    break
            break
    if not last_assistant:
        return None

    # 経過時間 check
    elapsed = None
    if last_assistant_ts:
        try:
            ts = datetime.fromisoformat(last_assistant_ts)
            if ts.tzinfo is None:
                ts = ts.replace(tzinfo=JST)
            elapsed = (datetime.now(JST) - ts).total_seconds()
            if elapsed > max_seconds:
                return None  # 古すぎる、別話題で再開扱い
            if elapsed < 0:
                return None  # 不正
        except Exception:
            pass

    return {
        "user_query": last_user_query or "",
        "bot_response": last_assistant,
        "continuation": current_text,
        "elapsed_seconds": elapsed,
    }


def _iter_records():
    if not SUCCESS_FILE.exists():
        return
    for line in SUCCESS_FILE.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            yield json.loads(line)
        except Exception:
            continue


def list_recent(limit: int = 50, since_days: int = 30) -> list[dict]:
    """直近 N 日 の captured 件、新しい順."""
    cutoff = datetime.now(JST) - timedelta(days=since_days)
    items = []
    for r in _iter_records():
        ts_str = r.get("timestamp", "")
        try:
            ts = datetime.fromisoformat(ts_str)
            if ts.tzinfo is None:
                ts = ts.replace(tzinfo=JST)
            if ts < cutoff:
                continue
        except Exception:
            pass
        items.append(r)
    items.sort(key=lambda r: r.get("timestamp", ""), reverse=True)
    return items[:limit]


def count_recent_days(days: int = 7) -> int:
    cutoff = datetime.now(JST) - timedelta(days=days)
    n = 0
    for r in _iter_records():
        ts_str = r.get("timestamp", "")
        try:
            ts = datetime.fromisoformat(ts_str)
            if ts.tzinfo is None:
                ts = ts.replace(tzinfo=JST)
            if ts >= cutoff:
                n += 1
        except Exception:
            pass
    return n


def update_status(fid: str, new_status: str) -> bool:
    """status 更新: captured → reviewed/applied/skipped."""
    if new_status not in ("captured", "reviewed", "applied", "skipped"):
        return False
    with locked(SUCCESS_FILE):
        items = read_jsonl(SUCCESS_FILE, logger)
        changed = False
        for r in items:
            if r.get("id") == fid:
                r["status"] = new_status
                r["updated"] = _now_iso()
                changed = True
        if changed:
            write_jsonl_atomic(SUCCESS_FILE, items)
    return changed


def summary_stats() -> dict:
    """count + 日別 trend (= dashboard 用)."""
    total = 0
    by_day: dict[str, int] = {}
    by_status: dict[str, int] = {"captured": 0, "reviewed": 0, "applied": 0, "skipped": 0}
    for r in _iter_records():
        total += 1
        ts_str = r.get("timestamp", "")
        day = ts_str[:10] if ts_str else "?"
        by_day[day] = by_day.get(day, 0) + 1
        st = r.get("status", "captured")
        if st in by_status:
            by_status[st] += 1
    return {
        "total": total,
        "by_status": by_status,
        "by_day": dict(sorted(by_day.items(), reverse=True)[:14]),  # 直近 14 日
    }
