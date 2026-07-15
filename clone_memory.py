"""
うみやまAI 個別ユーザメモリー
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

LINE Works で 1:1 DM してくる社員ごとに、累積メモリー (~1-2KB) を保持。
会話 1 ペアごとに LLM が背景で memory を増分更新 → 次回応答時に system prompt に注入。

これにより:
- 「先週の店舗売上について相談してた」記憶が引き継がれる
- 役職・所属本部・店舗が分かれば応答を最適化 (新人スタッフ vs 本部長で口調・内容が変わる)
- 進行中の悩み・話題が継続される (毎回ゼロから説明し直さなくて済む)

保存形式: Markdown (frontmatter + 4 セクション)
場所:    data/brain/clone_memory/<user_id>.md
権限:    海山さんのみ閲覧可 (/clone-memory コマンド経由)

セクション:
  ## Profile           — 役職、所属本部、店舗、エリア、経験年数 (推測含む)
  ## Ongoing Topics    — 進行中の悩み・話題 (古いものは LLM が削除)
  ## Key Facts         — 重要な事実 (本人が話した内容のみ)
  ## Preferences       — 応答スタイルの好み (短文 / 数字重視 / 詳細解説等)

プライバシー:
- 健康・家族・個人的な深刻な悩みは記録しない (LLM プロンプトで明示)
- 海山が必要に応じて個別ユーザの memory を確認・編集・削除可能
"""
from __future__ import annotations

import os
import json
import logging
from datetime import datetime
from pathlib import Path
from typing import Optional

from services._review_store import write_text_atomic

logger = logging.getLogger(__name__)

BRAIN_ROOT = Path(os.getenv("BRAIN_ROOT", "/app/data/brain"))
MEMORY_DIR = BRAIN_ROOT / "clone_memory"

DEFAULT_BODY = """## Profile
(不明、初回会話)

## Ongoing Topics
(まだなし)

## Key Facts
(まだなし)

## Preferences
(まだなし)
"""


def _ensure_dir():
    MEMORY_DIR.mkdir(parents=True, exist_ok=True)


def _user_file(user_id: str) -> Path:
    """user_id を安全なファイル名に変換"""
    safe = user_id.replace("/", "_").replace("..", "_")
    return MEMORY_DIR / f"{safe}.md"


def _split_frontmatter(content: str) -> tuple[dict, str]:
    """`---\nkey: value\n---\n本文` をパース → (frontmatter dict, body)"""
    if not content.startswith("---"):
        return {}, content
    end = content.find("\n---", 3)
    if end < 0:
        return {}, content
    fm_text = content[3:end].strip()
    body = content[end + 4 :].lstrip("\n")
    fm = {}
    for line in fm_text.splitlines():
        if ":" in line:
            k, v = line.split(":", 1)
            fm[k.strip()] = v.strip()
    return fm, body


def load(user_id: str) -> str:
    """ユーザメモリーの body を取得 (frontmatter 抜き、prompt 注入用)。
    存在しなければ DEFAULT_BODY を返す。"""
    path = _user_file(user_id)
    if not path.exists():
        return DEFAULT_BODY
    try:
        content = path.read_text(encoding="utf-8")
        _, body = _split_frontmatter(content)
        return body.strip() or DEFAULT_BODY
    except Exception as e:
        logger.warning(f"clone_memory.load failed: {e}")
        return DEFAULT_BODY


def load_with_meta(user_id: str) -> tuple[dict, str]:
    """frontmatter (dict) + body (str) を返す。管理 UI 用。"""
    path = _user_file(user_id)
    if not path.exists():
        return {}, DEFAULT_BODY
    try:
        content = path.read_text(encoding="utf-8")
        fm, body = _split_frontmatter(content)
        return fm, body.strip() or DEFAULT_BODY
    except Exception as e:
        logger.warning(f"clone_memory.load_with_meta failed: {e}")
        return {}, DEFAULT_BODY


def save(
    user_id: str,
    body: str,
    user_display: Optional[str] = None,
    turn_count: Optional[int] = None,
) -> None:
    """body を保存 (frontmatter を自動付加)"""
    _ensure_dir()
    path = _user_file(user_id)
    now = datetime.now().astimezone().isoformat(timespec="seconds")

    # 既存ファイルの turn_count を引き継ぐ
    existing_turn = 0
    if turn_count is None and path.exists():
        try:
            old_fm, _ = _split_frontmatter(path.read_text(encoding="utf-8"))
            existing_turn = int(old_fm.get("turn_count", "0"))
        except Exception:
            pass

    final_turn = turn_count if turn_count is not None else existing_turn

    header_lines = [
        "---",
        f"user_id: {user_id}",
        f"user_display: {user_display or ''}",
        f"updated: {now}",
        f"turn_count: {final_turn}",
        "---",
        "",
    ]
    # body の先頭 frontmatter を除去 (LLM が誤って付けて返した場合の安全策)
    body_clean = body.strip()
    if body_clean.startswith("---"):
        _, body_clean = _split_frontmatter(body_clean)
        body_clean = body_clean.strip()

    final = "\n".join(header_lines) + body_clean + "\n"
    try:
        # ★2026-06-10: torn write 防止に atomic 化。sleep agent との lost update は
        #   sleep 側の optimistic re-load check で防ぐ (async 単一プロセスゆえ flock は不採用)。
        write_text_atomic(path, final)
    except Exception as e:
        logger.exception(f"clone_memory.save failed for {user_id[:8]}: {e}")


def list_users() -> list[dict]:
    """全ユーザのメモリーサマリ (海山 /clone-memory 一覧用)"""
    _ensure_dir()
    out = []
    for f in sorted(MEMORY_DIR.glob("*.md")):
        try:
            content = f.read_text(encoding="utf-8")
            fm, body = _split_frontmatter(content)
            out.append(
                {
                    "user_id": f.stem,
                    "display": fm.get("user_display", ""),
                    "turn_count": int(fm.get("turn_count", "0") or "0"),
                    "size": len(body),
                    "last_updated": fm.get("updated", ""),
                    "mtime": f.stat().st_mtime,
                }
            )
        except Exception:
            continue
    out.sort(key=lambda x: x["mtime"], reverse=True)
    return out


def dump_user(user_id: str) -> str:
    """ユーザのメモリーを表示用 markdown で返す (/clone-memory <prefix>)"""
    path = _user_file(user_id)
    if not path.exists():
        return f"メモリーなし: {user_id[:8]}..."
    try:
        return path.read_text(encoding="utf-8")
    except Exception:
        return f"メモリー読込失敗: {user_id[:8]}..."


def forget(user_id: str) -> bool:
    """メモリーファイルを削除 (/clone-memory-forget)"""
    path = _user_file(user_id)
    if path.exists():
        path.unlink()
        return True
    return False


def find_users(prefix: str) -> list[str]:
    """user_id prefix に該当する user_id を返す (海山が短い prefix で指定するため)"""
    _ensure_dir()
    if not prefix:
        return []
    matches = []
    for f in MEMORY_DIR.glob("*.md"):
        if f.stem.startswith(prefix):
            matches.append(f.stem)
    return matches
