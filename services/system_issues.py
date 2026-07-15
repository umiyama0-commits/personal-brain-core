"""services/system_issues.py — システム修正依頼 (= bug / 機能要望) queue

★2026-05-25 海山指示: ダッシュボードの主目的 2 つの 1 つ
  1. うみやまAI の回答品質向上 (= clone_learning queue 経由)
  2. 不備によるシステム修正依頼 (= ここ、新 queue)

clone_learning と同様の interface:
  - add_entry(description, expected, reviewer)  → 新規登録
  - list_pending(limit)                         → pending list
  - update_status(fid, new_status)              → accept/reject/fixed
  - add_comment(fid, comment, reviewer)         → コメント追記

保存先: data/brain/clone_review/system_issues.jsonl (= 1 ファイル append-only)

record schema:
{
  "id": "sysi_<12 hex>",
  "timestamp": "2026-05-25T15:30:00+09:00",
  "reviewer": "umiyama",
  "category": "system_issue",
  "description": "提案 wiki patch を直接編集できる UI が欲しい",
  "expected": "textarea で patch を編集可能化",  # 任意
  "status": "pending|acknowledged|fixed|rejected",
  "comments": [{"ts": ..., "reviewer": ..., "comment": ...}, ...]
}
"""
from __future__ import annotations

import json
import logging
import os
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
ISSUES_DIR = BRAIN_ROOT / "clone_review"
ISSUES_FILE = ISSUES_DIR / "system_issues.jsonl"

VALID_STATUSES = {"pending", "acknowledged", "fixed", "rejected"}


def _ensure_dir() -> None:
    ISSUES_DIR.mkdir(parents=True, exist_ok=True)


def _now_iso() -> str:
    return datetime.now().astimezone().isoformat(timespec="seconds")


def add_entry(description: str, expected: str = "", reviewer: str = "umiyama") -> str:
    """新規 system issue を登録. 返り値: 採番された id."""
    _ensure_dir()
    description = (description or "").strip()
    if not description:
        raise ValueError("description は必須")
    rec = {
        "id": f"sysi_{uuid.uuid4().hex[:12]}",
        "timestamp": _now_iso(),
        "reviewer": reviewer,
        "category": "system_issue",
        "description": description,
        "expected": (expected or "").strip(),
        "status": "pending",
        "comments": [],
    }
    with locked(ISSUES_FILE):
        append_jsonl(ISSUES_FILE, rec)
    logger.info(f"system_issue added: {rec['id']} by {reviewer}")
    return rec["id"]


def _read_all() -> list[dict]:
    return read_jsonl(ISSUES_FILE, logger)


def _write_all(items: list[dict]) -> None:
    """全件 atomic 書き込み (lost update 防止は呼び出し側 locked() の責務)。"""
    write_jsonl_atomic(ISSUES_FILE, items)


def list_pending(limit: int = 30) -> list[dict]:
    """pending 状態の issue を新しい順に返す."""
    items = [r for r in _read_all() if r.get("status") == "pending"]
    items.sort(key=lambda r: r.get("timestamp", ""), reverse=True)
    return items[:limit]


def list_all(limit: int = 100, include_resolved: bool = False) -> list[dict]:
    """全 issue を新しい順に返す.

    include_resolved=False (= default): fixed / rejected は除外。
    """
    items = _read_all()
    if not include_resolved:
        items = [r for r in items if r.get("status") not in ("fixed", "rejected")]
    items.sort(key=lambda r: r.get("timestamp", ""), reverse=True)
    return items[:limit]


def update_status(fid: str, new_status: str) -> bool:
    """status を更新. 不正 status は False."""
    if new_status not in VALID_STATUSES:
        logger.warning(f"invalid status: {new_status}")
        return False
    with locked(ISSUES_FILE):
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
    """コメント追記. 最新 20 件まで保持."""
    comment = (comment or "").strip()
    if not comment:
        return False
    with locked(ISSUES_FILE):
        items = _read_all()
        changed = False
        for r in items:
            if r.get("id") == fid:
                comments = r.get("comments", [])
                comments.append({
                    "ts": _now_iso(),
                    "reviewer": reviewer,
                    "comment": comment,
                })
                r["comments"] = comments[-20:]
                r["updated"] = _now_iso()
                changed = True
                break
        if changed:
            _write_all(items)
    return changed


def count_pending() -> int:
    """top page 用: pending 件数を返す (= badge 表示)."""
    return sum(1 for r in _read_all() if r.get("status") == "pending")
