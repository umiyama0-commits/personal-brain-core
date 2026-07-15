"""services/data_gaps.py — 「データ無し」回答 = データ拡充候補 queue

★2026-05-26 海山指示: bot が「データ無い」回答した時に拾って queue 化 →
ダッシュボードで「これは整備する / skip」 判定 → データ拡充の改善 cycle を回す。

検出は scripts/data_gap_detector.py で行い、ここは queue + dashboard 側。

保存先: data/brain/clone_review/data_gaps.jsonl (= append-only)

record schema:
{
  "id": "gap_<12 hex>",
  "timestamp": "2026-05-26T...",
  "user_id": "<truncated>",
  "user_query": "武蔵小山の去年の客単価は?",       # query 全文 (最大 300字)
  "bot_response": "申し訳ありません、その日次データはまだ...",  # 応答頭 600字
  "matched_category": "no_data",                    # 検出 pattern label
  "matched_text": "データがない",                   # 該当箇所
  "forward_looking": true,                          # 既に「今後拡充」 tone を含むか
  "occurrence_count": 1,                            # 同 query で何回出たか (= dedupe + 集計)
  "status": "pending|planned|done|skipped",
  "comments": [...]
}

de-dupe: 同じ user_query + matched_category なら 既存 record の occurrence_count を +1
(= 「武蔵小山の去年の客単価は?」 が 5 回聞かれてる = 優先度高、と一発で見える)
"""
from __future__ import annotations

import json
import logging
import os
import re
import uuid
from datetime import datetime
from pathlib import Path

from services._review_store import (
    append_jsonl,
    locked,
    read_jsonl,
    write_jsonl_atomic,
)

logger = logging.getLogger(__name__)

BRAIN_ROOT = Path(os.getenv("BRAIN_ROOT", "/app/data/brain"))
GAPS_DIR = BRAIN_ROOT / "clone_review"
GAPS_FILE = GAPS_DIR / "data_gaps.jsonl"

VALID_STATUSES = {"pending", "planned", "done", "skipped"}


def _ensure_dir() -> None:
    GAPS_DIR.mkdir(parents=True, exist_ok=True)


def _now_iso() -> str:
    return datetime.now().astimezone().isoformat(timespec="seconds")


def _normalize_query(q: str) -> str:
    """de-dupe 用に query を正規化 (= 空白統一 + 数字を <N> に置換).

    例: 「武蔵小山の去年の客単価は?」 と 「吉祥寺の去年の客単価は?」 は別 query 扱い、
    でも 「直近 3 日の」 と 「直近 7 日の」 は同 pattern (= 数字 mask) で同一視。
    """
    q = re.sub(r"\s+", " ", q.strip())
    q = re.sub(r"\d+", "<N>", q)
    return q[:300].lower()


def _read_all() -> list[dict]:
    return read_jsonl(GAPS_FILE, logger)


def _write_all(items: list[dict]) -> None:
    """全件 atomic 書き込み (lost update 防止は呼び出し側 locked() の責務)。"""
    write_jsonl_atomic(GAPS_FILE, items)


def auto_capture(user_query: str, bot_response: str, user_id: str = "",
                 matched_category: str = "", matched_text: str = "",
                 forward_looking: bool = False) -> str:
    """bot 応答 hook から呼ぶ自動 capture (= detect 後 即 enqueue).

    de-dupe: 同 normalized_query + matched_category なら occurrence_count +1。
    Returns: record id (新規 or 既存)
    """
    _ensure_dir()
    user_query = (user_query or "").strip()[:300]
    bot_response = (bot_response or "").strip()[:600]
    if not user_query:
        return ""

    norm_q = _normalize_query(user_query)
    with locked(GAPS_FILE):
        items = _read_all()

        # 既存 同 norm_q + category 探索
        for r in items:
            if r.get("status") in ("done", "skipped"):
                continue  # 解決済は dedupe 対象外、新規 capture
            if (_normalize_query(r.get("user_query", "")) == norm_q
                    and r.get("matched_category", "") == matched_category):
                # increment + 最新 timestamp 更新
                r["occurrence_count"] = int(r.get("occurrence_count", 1)) + 1
                r["updated"] = _now_iso()
                r["bot_response"] = bot_response  # latest sample
                r["forward_looking"] = bool(forward_looking)
                _write_all(items)
                logger.info(f"data_gap incremented: {r['id']} (count={r['occurrence_count']})")
                return r["id"]

        # 新規
        rec = {
            "id": f"gap_{uuid.uuid4().hex[:12]}",
            "timestamp": _now_iso(),
            "user_id": (user_id or "")[:16],
            "user_query": user_query,
            "bot_response": bot_response,
            "matched_category": matched_category,
            "matched_text": matched_text,
            "forward_looking": bool(forward_looking),
            "occurrence_count": 1,
            "status": "pending",
            "comments": [],
        }
        append_jsonl(GAPS_FILE, rec)
    logger.info(f"data_gap captured: {rec['id']} cat={matched_category}")
    return rec["id"]


def list_active(limit: int = 50) -> list[dict]:
    """pending + planned を新しい順 (= occurrence_count desc + timestamp desc)."""
    items = [r for r in _read_all() if r.get("status") in ("pending", "planned")]
    items.sort(key=lambda r: (-int(r.get("occurrence_count", 1)), r.get("timestamp", "")), reverse=False)
    # occurrence 降順、tie 時 timestamp 降順 = sort key 工夫 (= 簡単に: 2 段 sort)
    items.sort(key=lambda r: r.get("timestamp", ""), reverse=True)
    items.sort(key=lambda r: -int(r.get("occurrence_count", 1)))
    return items[:limit]


def list_all(limit: int = 100, include_resolved: bool = False) -> list[dict]:
    items = _read_all()
    if not include_resolved:
        items = [r for r in items if r.get("status") not in ("done", "skipped")]
    items.sort(key=lambda r: r.get("timestamp", ""), reverse=True)
    return items[:limit]


def update_status(fid: str, new_status: str) -> bool:
    if new_status not in VALID_STATUSES:
        return False
    with locked(GAPS_FILE):
        items = _read_all()
        changed = False
        for r in items:
            if r.get("id") == fid:
                r["status"] = new_status
                r["updated"] = _now_iso()
                changed = True
                break
        if changed:
            _write_all(items)
    return changed


def add_comment(fid: str, comment: str, reviewer: str = "umiyama") -> bool:
    comment = (comment or "").strip()
    if not comment:
        return False
    with locked(GAPS_FILE):
        items = _read_all()
        changed = False
        for r in items:
            if r.get("id") == fid:
                comments = r.get("comments", [])
                comments.append({"ts": _now_iso(), "reviewer": reviewer, "comment": comment})
                r["comments"] = comments[-20:]
                r["updated"] = _now_iso()
                changed = True
                break
        if changed:
            _write_all(items)
    return changed


def count_active() -> int:
    return sum(1 for r in _read_all() if r.get("status") in ("pending", "planned"))


def summary_by_category() -> dict:
    """category 別 集計 (= dashboard summary 用)."""
    items = _read_all()
    by_cat: dict[str, dict] = {}
    for r in items:
        cat = r.get("matched_category", "unknown")
        if cat not in by_cat:
            by_cat[cat] = {"pending": 0, "planned": 0, "done": 0, "skipped": 0, "occurrences": 0}
        st = r.get("status", "pending")
        if st in by_cat[cat]:
            by_cat[cat][st] += 1
        by_cat[cat]["occurrences"] += int(r.get("occurrence_count", 1))
    return by_cat
