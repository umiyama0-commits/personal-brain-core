"""services/web_clips.py — Web / 他媒体から拾った言葉・考えを wiki に取り込む queue

★2026-05-26 海山指示: 「web 等で拾ってきた考え方や言葉を自分の考えの wiki に反映したいのだけど、
ダッシュボードから入れ込む機能を追加して」

設計:
- ダッシュボード から直接入力 (= title + source URL + 引用本文 + 自分の感想 + 反映先 wiki)
- pending 状態で queue に保存、review 後 wiki に追記 (= apply)
- 反映先 wiki は predefined choices (= 既存 8 次元 interview + core 3 種)
- 既存 alignment_interview の蒸留 wiki と並走 (= 別 source、同じ管理棚)

保存先: data/brain/clone_review/web_clips.jsonl (= append-only)

record schema:
{
  "id": "clip_<12 hex>",
  "timestamp": "2026-05-26T15:30:00+09:00",
  "reviewer": "umiyama",
  "title": "Naval の信仰の話",                    # optional だが推奨
  "source_url": "https://...",                    # optional
  "quote": "引用本文",                            # 必須
  "reflection": "これに共感した理由",             # optional (= 海山の加筆)
  "target_wiki": "interview/value-roots.md",      # 必須、WIKI_TARGETS から選択
  "status": "pending|applied|rejected",
  "comments": [...]
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
CLIPS_DIR = BRAIN_ROOT / "clone_review"
CLIPS_FILE = CLIPS_DIR / "web_clips.jsonl"
WIKI_DIR = BRAIN_ROOT / "wiki"

VALID_STATUSES = {"pending", "applied", "rejected"}

# ★反映先 wiki の predefined choices (= 安全策、任意 path 不可)
# (path, label, visibility_warning)
WIKI_TARGETS: list[tuple[str, str, str]] = [
    # ─── interview/* (= 全 private、本人像専用) ────────
    ("interview/biography.md", "biography — 過去・原体験 (private)", ""),
    ("interview/value-roots.md", "value roots — 価値観の根 (private)", ""),
    ("interview/judgment.md", "judgment — 判断の癖 (private)", ""),
    ("interview/reflex.md", "reflex — 感情・反射 (private)", ""),
    ("interview/aesthetics.md", "aesthetics — 美意識・感覚 (private)", ""),
    ("interview/philosophy.md", "philosophy — 哲学・死生観 (private)", ""),
    ("interview/style.md", "style (interview) — 言い回し・語彙 (private)", ""),
    ("interview/relationships.md", "relationships — 関係性の機微 (private)", ""),
    ("interview/embodiment.md", "embodiment — 身体・習慣 (private)", ""),
    # ─── core public (= 社員 bot にも影響、warning) ─────
    ("knowledge/web-clips-general.md", "knowledge/web-clips-general — 雑な引用置き場 (public、後で昇格判断)", ""),
    ("thinking.md", "thinking — 思考様式 core (★public、社員 bot にも影響)", "WARNING"),
    ("style.md", "style — 応答スタイル core (★public、社員 bot にも影響)", "WARNING"),
    ("identity.md", "identity — 自分とは core (★public、社員 bot にも影響)", "WARNING"),
]
WIKI_TARGET_PATHS = {t[0] for t in WIKI_TARGETS}


def _ensure_dir() -> None:
    CLIPS_DIR.mkdir(parents=True, exist_ok=True)


def _now_iso() -> str:
    return datetime.now().astimezone().isoformat(timespec="seconds")


def add_clip(
    quote: str,
    target_wiki: str,
    title: str = "",
    source_url: str = "",
    reflection: str = "",
    reviewer: str = "umiyama",
) -> str:
    """新 clip 登録. 返り値: 採番された id."""
    _ensure_dir()
    quote = (quote or "").strip()
    if not quote:
        raise ValueError("quote 必須")
    if target_wiki not in WIKI_TARGET_PATHS:
        raise ValueError(f"invalid target_wiki: {target_wiki} (allowed: {sorted(WIKI_TARGET_PATHS)})")
    rec = {
        "id": f"clip_{uuid.uuid4().hex[:12]}",
        "timestamp": _now_iso(),
        "reviewer": reviewer,
        "title": (title or "").strip(),
        "source_url": (source_url or "").strip(),
        "quote": quote,
        "reflection": (reflection or "").strip(),
        "target_wiki": target_wiki,
        "status": "pending",
        "comments": [],
    }
    with locked(CLIPS_FILE):
        append_jsonl(CLIPS_FILE, rec)
    logger.info(f"web_clip added: {rec['id']} → {target_wiki} by {reviewer}")
    return rec["id"]


def _read_all() -> list[dict]:
    return read_jsonl(CLIPS_FILE, logger)


def _write_all(items: list[dict]) -> None:
    """全件 atomic 書き込み (lost update 防止は呼び出し側 locked() の責務)。"""
    write_jsonl_atomic(CLIPS_FILE, items)


def list_pending(limit: int = 50) -> list[dict]:
    items = [r for r in _read_all() if r.get("status") == "pending"]
    items.sort(key=lambda r: r.get("timestamp", ""), reverse=True)
    return items[:limit]


def list_all(limit: int = 100, include_resolved: bool = False) -> list[dict]:
    items = _read_all()
    if not include_resolved:
        items = [r for r in items if r.get("status") not in ("applied", "rejected")]
    items.sort(key=lambda r: r.get("timestamp", ""), reverse=True)
    return items[:limit]


def update_status(fid: str, new_status: str) -> bool:
    if new_status not in VALID_STATUSES:
        return False
    with locked(CLIPS_FILE):
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


def update_clip(fid: str, **fields) -> bool:
    """edit (= title / quote / reflection / target_wiki を更新可能)."""
    allowed = {"title", "quote", "reflection", "target_wiki", "source_url"}
    with locked(CLIPS_FILE):
        items = _read_all()
        changed = False
        for r in items:
            if r.get("id") != fid:
                continue
            if r.get("status") != "pending":
                return False  # applied / rejected は edit 不可
            for k, v in fields.items():
                if k not in allowed:
                    continue
                if k == "target_wiki" and v not in WIKI_TARGET_PATHS:
                    continue  # 不正 path は skip
                r[k] = (v or "").strip() if isinstance(v, str) else v
                changed = True
            if changed:
                r["updated"] = _now_iso()
            break
        if changed:
            _write_all(items)
    return changed


def add_comment(fid: str, comment: str, reviewer: str = "umiyama") -> bool:
    comment = (comment or "").strip()
    if not comment:
        return False
    with locked(CLIPS_FILE):
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


def find_clip(fid: str) -> dict | None:
    for r in _read_all():
        if r.get("id") == fid:
            return r
    return None


def apply_clip(fid: str) -> dict:
    """clip を target_wiki に追記、status=applied に更新.

    Returns: {"ok": bool, "applied_path": str | None, "error": str | None}
    """
    clip = find_clip(fid)
    if not clip:
        return {"ok": False, "error": f"clip not found: {fid}"}
    if clip.get("status") != "pending":
        return {"ok": False, "error": f"already {clip.get('status')}"}

    target = clip.get("target_wiki")
    if target not in WIKI_TARGET_PATHS:
        return {"ok": False, "error": f"invalid target_wiki: {target}"}

    path = WIKI_DIR / target
    path.parent.mkdir(parents=True, exist_ok=True)

    # 既存ファイルが無ければ minimal frontmatter で初期化
    if not path.exists():
        is_private = target.startswith("interview/")
        visibility = "private" if is_private else "public"
        exit_vis = "private" if is_private else "internal"
        path.write_text(
            "---\n"
            f"updated: {_now_iso()[:10]}\n"
            "confidence: medium\n"
            "tags: [web-clip, 取込]\n"
            "sources: [web、ダッシュボード取込]\n"
            f"clone_visibility: {visibility}\n"
            f"exit_visibility: {exit_vis}\n"
            "---\n"
            f"# {target}\n\n"
            "海山がダッシュボード経由で web / 他媒体から取り込んだ引用 + 感想。\n\n",
            encoding="utf-8",
        )

    # 追記 block を組み立て (= H3 + 引用 + 感想 + source)
    ts = (clip.get("timestamp") or _now_iso())[:10]
    title = clip.get("title", "").strip() or "(無題)"
    quote = clip.get("quote", "").strip()
    reflection = clip.get("reflection", "").strip()
    src = clip.get("source_url", "").strip()

    block_parts = [f"\n### [{ts}] {title}", ""]
    if quote:
        # multi-line quote
        block_parts.append("> " + quote.replace("\n", "\n> "))
        block_parts.append("")
    if reflection:
        block_parts.append(f"**海山の感想**: {reflection}")
        block_parts.append("")
    if src:
        block_parts.append(f"出典: <{src}>")
        block_parts.append("")
    block = "\n".join(block_parts)

    with path.open("a", encoding="utf-8") as f:
        f.write(block)

    # status 更新
    items = _read_all()
    for r in items:
        if r.get("id") == fid:
            r["status"] = "applied"
            r["applied_at"] = _now_iso()
            r["applied_path"] = target
            break
    _write_all(items)

    return {"ok": True, "applied_path": target}


def count_pending() -> int:
    return sum(1 for r in _read_all() if r.get("status") == "pending")
