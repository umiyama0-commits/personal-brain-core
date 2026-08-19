"""
うみやまAI 海山 daily audit store (★2026-05-24 Feature 3/4)
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

# 役割

海山が bot 応答に対して 1-click 評価を付け、品質 closed loop の center にする。
clone_feedback.py (= 社員からの修正希望) と相補:
- clone_feedback: 社員 → bot 「違う」「正しくは」検出、Wiki 反映候補化
- clone_audit (本 module): **海山自身**による bot 応答 review、ground truth signal

# UX (= 1-click 化、海山個人 LINE 経由)

## Pattern A: 海山個人 LINE で自分の bot 応答に直接 rating

海山: /clone-public 売上どうですか?
bot:  今日の全社売上は 20M、客数 1,228 です。
海山: ○          ← または 👍 / good / ok
bot:  ✓ audit 記録 (verdict=good)

## Pattern B: production うみやまAI (= LINE WORKS) を事後 audit

海山個人 LINE: /audit-recent
bot: 直近 10 件:
  [1] 13:25 田中: 売上? → "今日 20M、客数 1228" (DM)
  [2] 13:30 鈴木: 龍仁進捗? → "店長候補 3 名選考中" (group:ch_abc)
  [3] ...
海山: × 1 数字古い、最新は 22M
bot: ✓ #1 audit 記録: verdict=bad, note=数字古い、最新は 22M

# 保存

  data/brain/clone_audit/YYYY-MM-DD.jsonl   — 日別 audit 記録
  data/brain/clone_audit/.audited_msg_ids.json — audit 済 message id set (= 重複 audit 防止)

# Record 形式

  {
    "id": "2026-05-24_001",
    "timestamp": "2026-05-24T14:30:00+09:00",
    "audited_by": "海山 LINE user_id",
    "target_user_id": "対象 bot reply の発言主 (= 海山なら自身、別なら社員)",
    "target_channel_id": null | "channel_id",
    "user_query": "bot が応答した user の質問",
    "bot_response": "bot 応答 (= audit 対象)",
    "verdict": "good" | "bad" | "fix",
    "note": "fix の場合の修正内容、good/bad は省略可",
    "ts_target": "bot 応答の timestamp"
  }

# verdict 凡例

  good (= ○ / 👍): 海山判断と一致、正しい。Wiki 反映 OK。
  bad  (= × / 👎): 海山判断と異なる、誤り。要 fix、Wiki 反映禁止。
  fix  (= ! / 修正): 部分修正必要、note に「正しくは XXX」記載。
"""
from __future__ import annotations

import os
import json
import logging
from datetime import datetime, timedelta
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)

BRAIN_ROOT = Path(os.getenv("BRAIN_ROOT", "/app/data/brain"))
AUDIT_DIR = BRAIN_ROOT / "clone_audit"
AUDITED_IDS_FILE = AUDIT_DIR / ".audited_msg_ids.json"


def _ensure_dir():
    AUDIT_DIR.mkdir(parents=True, exist_ok=True)


def _today_file() -> Path:
    return AUDIT_DIR / f"{datetime.now().strftime('%Y-%m-%d')}.jsonl"


def _load_audited_ids() -> set[str]:
    """audit 済 message id を取得 (= 重複 audit 防止)."""
    if not AUDITED_IDS_FILE.exists():
        return set()
    try:
        return set(json.loads(AUDITED_IDS_FILE.read_text(encoding="utf-8")))
    except Exception:
        return set()


def _save_audited_ids(ids: set[str]):
    _ensure_dir()
    AUDITED_IDS_FILE.write_text(
        json.dumps(sorted(ids), ensure_ascii=False), encoding="utf-8"
    )


def _make_msg_id(ts: str, user_id: str, bot_response: str) -> str:
    """audit 対象 message を一意特定する id (= ts + user_id + response 先頭 hash)."""
    import hashlib
    h = hashlib.sha1((bot_response or "")[:200].encode("utf-8")).hexdigest()[:8]
    return f"{ts}_{user_id[:8]}_{h}"


# ─── verdict normalize ────────────────────────────
VERDICT_GOOD = {"○", "◯", "👍", "good", "ok", "OK", "yes", "正"}
VERDICT_BAD = {"×", "✕", "❌", "👎", "bad", "ng", "NG", "no", "誤"}
VERDICT_FIX = {"!", "fix", "修正", "訂正"}


def parse_verdict_prefix(text: str) -> Optional[tuple[str, str]]:
    """text の先頭が verdict prefix かチェック → (verdict, rest) or None.

    Examples:
      "○"                  → ("good", "")
      "× 数字古い"         → ("bad", "数字古い")
      "! 正しくは 22M"     → ("fix", "正しくは 22M")
      "○ 1"                → ("good", "1")
      "fix 3 正しくは ..."  → ("fix", "3 正しくは ...")
      "普通のメッセージ"     → None
    """
    if not text:
        return None
    text = text.strip()
    if not text:
        return None

    # 1 文字 verdict (= "○" 単体)
    first = text[0]
    if first in {"○", "◯", "×", "✕", "❌", "!"}:
        rest = text[1:].strip()
        if first in {"○", "◯"}:
            return ("good", rest)
        if first in {"×", "✕", "❌"}:
            return ("bad", rest)
        if first == "!":
            return ("fix", rest)

    # 2-3 文字 emoji (= "👍" 等、ord >= 0x1F300 範囲)
    if len(text) >= 1:
        # 絵文字 1 文字分判定 (= surrogate pair 簡易対応)
        if text.startswith("👍"):
            return ("good", text[len("👍"):].strip())
        if text.startswith("👎"):
            return ("bad", text[len("👎"):].strip())

    # 単語 prefix (= space 区切り)
    parts = text.split(maxsplit=1)
    word = parts[0]
    rest = parts[1] if len(parts) > 1 else ""
    if word in VERDICT_GOOD:
        return ("good", rest)
    if word in VERDICT_BAD:
        return ("bad", rest)
    if word in VERDICT_FIX:
        return ("fix", rest)

    return None


def record_audit(
    audited_by: str,
    target_user_id: str,
    user_query: str,
    bot_response: str,
    verdict: str,
    note: str = "",
    target_channel_id: Optional[str] = None,
    ts_target: Optional[str] = None,
) -> dict:
    """1 件の audit を save."""
    _ensure_dir()
    if verdict not in ("good", "bad", "fix"):
        raise ValueError(f"verdict must be good|bad|fix, got {verdict!r}")

    now_iso = datetime.now().astimezone().isoformat(timespec="seconds")
    ts_target = ts_target or now_iso
    msg_id = _make_msg_id(ts_target, target_user_id, bot_response)

    # 重複 audit chk (= 同 msg を 2 度 audit したら overwrite ではなく追加 record)
    audited = _load_audited_ids()
    audited.add(msg_id)
    _save_audited_ids(audited)

    rec = {
        "id": f"{datetime.now().strftime('%Y-%m-%d')}_{len(audited):04d}",
        "msg_id": msg_id,
        "timestamp": now_iso,
        "audited_by": audited_by,
        "target_user_id": target_user_id,
        "target_channel_id": target_channel_id,
        "user_query": user_query[:1000],
        "bot_response": bot_response[:2000],
        "verdict": verdict,
        "note": note[:1000],
        "ts_target": ts_target,
    }

    path = _today_file()
    with path.open("a", encoding="utf-8") as f:
        f.write(json.dumps(rec, ensure_ascii=False) + "\n")

    logger.info(
        f"audit recorded: id={rec['id']} verdict={verdict} "
        f"target_user={target_user_id[:8]} channel={target_channel_id or 'DM'}"
    )
    return rec


def is_msg_audited(ts: str, user_id: str, bot_response: str) -> bool:
    """同じ bot reply が既に audit 済か判定 (= 重複 audit 防止 UI 用)."""
    msg_id = _make_msg_id(ts, user_id, bot_response)
    return msg_id in _load_audited_ids()


def mark_resolved(record_id: str, resolved_by: str = "umiyama", note: str = "") -> bool:
    """★2026-05-26 海山指示: 要 attention list から「対応済」として閉じる.

    audit record に resolved=True + resolved_at + resolved_by を付加。
    audit_stats() の needs_attention 計算で除外されるようになる。
    """
    if not record_id:
        return False
    _ensure_dir()
    for f in sorted(AUDIT_DIR.glob("*.jsonl"), reverse=True):
        try:
            lines = f.read_text(encoding="utf-8").splitlines()
        except Exception:
            continue
        changed = False
        new_lines = []
        for line in lines:
            line = line.strip()
            if not line:
                continue
            try:
                r = json.loads(line)
                if r.get("id") == record_id:
                    r["resolved"] = True
                    r["resolved_at"] = datetime.now().astimezone().isoformat(timespec="seconds")
                    r["resolved_by"] = resolved_by
                    if note:
                        existing = (r.get("note") or "").strip()
                        sep = " | " if existing else ""
                        r["note"] = f"{existing}{sep}[resolved {datetime.now().date()}] {note}"
                    changed = True
                new_lines.append(json.dumps(r, ensure_ascii=False))
            except Exception:
                new_lines.append(line)
        if changed:
            f.write_text("\n".join(new_lines) + "\n", encoding="utf-8")
            return True
    return False


# ─── 集計 ─────────────────────────────────────────
def audit_stats(days: int = 30) -> dict:
    """過去 N 日の audit 統計."""
    _ensure_dir()
    cutoff = datetime.now() - timedelta(days=days)
    records = []
    for f in sorted(AUDIT_DIR.glob("*.jsonl")):
        try:
            date_str = f.stem
            file_date = datetime.strptime(date_str, "%Y-%m-%d")
            if file_date < cutoff:
                continue
        except Exception:
            continue
        try:
            for line in f.read_text(encoding="utf-8").splitlines():
                line = line.strip()
                if not line:
                    continue
                try:
                    records.append(json.loads(line))
                except Exception:
                    continue
        except Exception:
            continue

    n_total = len(records)
    n_good = sum(1 for r in records if r.get("verdict") == "good")
    n_bad = sum(1 for r in records if r.get("verdict") == "bad")
    n_fix = sum(1 for r in records if r.get("verdict") == "fix")
    good_rate_pct = round((n_good / n_total * 100), 1) if n_total else 0
    # bad + fix の recent samples (= 改善 candidate)
    # ★2026-05-26 海山指示: resolved=True の record は除外 (= 対応済として閉じれる仕組み)
    needs_attention = [
        {
            "id": r.get("id"),
            "ts": r.get("timestamp", "")[:19],
            "verdict": r.get("verdict"),
            "user_query": r.get("user_query", "")[:80],
            "bot_response": r.get("bot_response", "")[:80],
            "note": r.get("note", "")[:120],
        }
        for r in records
        if r.get("verdict") in ("bad", "fix") and not r.get("resolved")
    ][-20:]

    return {
        "window_days": days,
        "n_total_audits": n_total,
        "n_good": n_good,
        "n_bad": n_bad,
        "n_fix": n_fix,
        "good_rate_pct": good_rate_pct,
        "needs_attention": needs_attention,
        "audited_msg_ids_count": len(_load_audited_ids()),
    }


def list_recent_unrated(
    user_id_filter: Optional[str] = None,
    limit: int = 10,
) -> list[dict]:
    """clone_history から最近の bot 応答で未 audit のものを返す (= /audit-recent 用).

    Args:
        user_id_filter: 特定 user の応答だけ (= None なら全 user)
        limit: 最大件数

    Returns:
        [{"index": int, "ts": str, "user_id": str, "user_display": str,
          "channel_id": str|None, "user_query": str, "bot_response": str}, ...]
    """
    try:
        import clone_history
    except Exception:
        return []

    # 全 user の history を統合 (= 直近 200 件まで)
    all_records = []
    history_dir = BRAIN_ROOT / "clone_history"
    if not history_dir.exists():
        return []

    for f in history_dir.glob("*.jsonl"):
        try:
            user_id = f.stem
            if user_id_filter and user_id != user_id_filter:
                continue
            lines = f.read_text(encoding="utf-8").splitlines()[-50:]
            for line in lines:
                line = line.strip()
                if not line:
                    continue
                try:
                    r = json.loads(line)
                    r["_user_id"] = user_id
                    all_records.append(r)
                except Exception:
                    continue
        except Exception:
            continue

    # ts 降順 sort
    all_records.sort(key=lambda r: r.get("timestamp", ""), reverse=True)

    # bot reply (= role=assistant) + その直前 user query を pair で抽出、未 audit のみ
    audited_ids = _load_audited_ids()
    unrated = []
    seen_msg_ids: set[str] = set()
    for i, r in enumerate(all_records):
        if r.get("role") != "assistant":
            continue
        ts = r.get("timestamp", "")
        user_id = r.get("_user_id", "")
        bot_response = r.get("text", "")
        # dedup は ts 単独でなく msg_id (= ts + user_id + response hash) で判定。
        # clone_history の timestamp は秒精度 (isoformat timespec="seconds") のため、
        # 別 user / 別内容の 2 応答が同一秒に記録されると ts 単独 dedup では後発 1 件が
        # 無音で脱落 (= /audit-recent から消える)。msg_id 判定なら真の重複だけ畳む。
        msg_id = _make_msg_id(ts, user_id, bot_response)
        if msg_id in seen_msg_ids:
            continue
        seen_msg_ids.add(msg_id)
        if msg_id in audited_ids:
            continue
        # 直前 user query 探す (= 同 user 直前の user role)
        user_query = ""
        for r2 in all_records[i:]:
            if r2.get("_user_id") == user_id and r2.get("role") == "user":
                if r2.get("timestamp", "") < ts:
                    user_query = r2.get("text", "")
                    break
        unrated.append({
            "index": len(unrated) + 1,
            "ts": ts,
            "user_id": user_id,
            "user_display": r.get("user_display", ""),
            "channel_id": r.get("channel_id"),
            "user_query": user_query[:200],
            "bot_response": bot_response[:300],
        })
        if len(unrated) >= limit:
            break
    return unrated
