"""
うみやまAI Group Context Memory (★2026-05-24 Tier 0: LINE WORKS group 対応)
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

LINE WORKS の **group (channel)** ごとに、累積文脈 (~1-2KB) を保持。
clone_memory.py が per-user memory なのに対し、本 module は per-channel memory。
両者は orthogonal で、bot 応答時には両方注入される (= hybrid memory model)。

# 設計

- file: data/brain/clone_group_context/<channel_id>.md
- 構造: frontmatter + 4 section
  - Group Profile      — group の目的 / メンバー一覧 / 用途
  - Ongoing Topics     — group で議論中の話題 (時間軸 metadata 付き)
  - Recent Events      — 重要な集団 event (時系列)
  - Group Culture      — 集団の応答スタイル好み (= 数字 first / 短文 / 結論先出し 等)
- 更新: group message 毎に fast-gpt 即時 + 30s idle で smart 全体再整理 (= sleep_time_agent 拡張)

# Privacy 境界 (= 厳格)

- 1:1 DM 内の発言は group context に絶対漏らさない
- group 内の 他 user 発言は group context にのみ入る (= 「○○さんが言った」は group 文脈で OK)
- group context を 1:1 DM の応答時に inject しない (= channel_id がある時のみ inject)

# 既存 clone_memory.py との関係

| 項目          | per-user (clone_memory)        | per-group (本 module)            |
|--------------|--------------------------------|----------------------------------|
| key          | user_id                        | channel_id                       |
| Profile      | 役職 / 嗜好 / 個人事情          | group 目的 / メンバー / 用途      |
| Ongoing      | user 個人の悩み / 関心          | group で議論中の topic           |
| Facts/Events | per-user 不変事実              | Recent Events (= 時系列 group 事象) |
| Preferences  | 応答スタイル好み                | Group Culture (= 集団の好み)     |
| 注入対象     | user_id 一致時                 | channel_id 一致時                 |

# 使い方 (= brain_wiki.py から)

```python
import clone_group_context
fm, body = clone_group_context.load_with_meta(channel_id)
turn_count = int(fm.get("turn_count", "0") or "0")
if turn_count > 0 and body:
    group_block = f"\n\n## このグループについて...\n\n{body}\n"
```
"""
from __future__ import annotations

import os
import logging
from datetime import datetime
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)

BRAIN_ROOT = Path(os.getenv("BRAIN_ROOT", "/app/data/brain"))
GROUP_CONTEXT_DIR = BRAIN_ROOT / "clone_group_context"

DEFAULT_BODY = """## Group Profile
(不明、初回 group message)

## Ongoing Topics
(まだなし)

## Recent Events
(まだなし)

## Group Culture
(まだなし)
"""


def _ensure_dir():
    GROUP_CONTEXT_DIR.mkdir(parents=True, exist_ok=True)


def _channel_file(channel_id: str) -> Path:
    """channel_id を安全なファイル名に変換 (= clone_memory と同規約)"""
    safe = channel_id.replace("/", "_").replace("..", "_")
    return GROUP_CONTEXT_DIR / f"{safe}.md"


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


def load(channel_id: str) -> str:
    """group context の body を取得 (frontmatter 抜き、prompt 注入用)。
    存在しなければ DEFAULT_BODY を返す。"""
    path = _channel_file(channel_id)
    if not path.exists():
        return DEFAULT_BODY
    try:
        content = path.read_text(encoding="utf-8")
        _, body = _split_frontmatter(content)
        return body.strip() or DEFAULT_BODY
    except Exception as e:
        logger.warning(f"clone_group_context.load failed: {e}")
        return DEFAULT_BODY


def load_with_meta(channel_id: str) -> tuple[dict, str]:
    """frontmatter (dict) + body (str) を返す。管理 UI 用。"""
    path = _channel_file(channel_id)
    if not path.exists():
        return {}, DEFAULT_BODY
    try:
        content = path.read_text(encoding="utf-8")
        fm, body = _split_frontmatter(content)
        return fm, body.strip() or DEFAULT_BODY
    except Exception as e:
        logger.warning(f"clone_group_context.load_with_meta failed: {e}")
        return {}, DEFAULT_BODY


def save(
    channel_id: str,
    body: str,
    channel_display: Optional[str] = None,
    turn_count: Optional[int] = None,
    member_count: Optional[int] = None,
) -> None:
    """body を保存 (frontmatter を自動付加)"""
    _ensure_dir()
    path = _channel_file(channel_id)
    now = datetime.now().astimezone().isoformat(timespec="seconds")

    # 既存ファイルの turn_count / member_count を引き継ぐ
    existing_turn = 0
    existing_member = 0
    if path.exists():
        try:
            old_fm, _ = _split_frontmatter(path.read_text(encoding="utf-8"))
            existing_turn = int(old_fm.get("turn_count", "0") or "0")
            existing_member = int(old_fm.get("member_count", "0") or "0")
        except Exception:
            pass

    final_turn = turn_count if turn_count is not None else existing_turn
    final_member = member_count if member_count is not None else existing_member

    header_lines = [
        "---",
        f"channel_id: {channel_id}",
        f"channel_display: {channel_display or ''}",
        f"updated: {now}",
        f"turn_count: {final_turn}",
        f"member_count: {final_member}",
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
        path.write_text(final, encoding="utf-8")
    except Exception as e:
        logger.exception(f"clone_group_context.save failed for {channel_id[:8]}: {e}")


def list_channels() -> list[dict]:
    """全 channel の context summary (海山 /clone-group 一覧用)"""
    _ensure_dir()
    out = []
    for f in sorted(GROUP_CONTEXT_DIR.glob("*.md")):
        try:
            content = f.read_text(encoding="utf-8")
            fm, body = _split_frontmatter(content)
            out.append(
                {
                    "channel_id": f.stem,
                    "display": fm.get("channel_display", ""),
                    "turn_count": int(fm.get("turn_count", "0") or "0"),
                    "member_count": int(fm.get("member_count", "0") or "0"),
                    "size": len(body),
                    "last_updated": fm.get("updated", ""),
                    "mtime": f.stat().st_mtime,
                }
            )
        except Exception:
            continue
    out.sort(key=lambda x: x["mtime"], reverse=True)
    return out


def dump_channel(channel_id: str) -> str:
    """channel の context を表示用 markdown で返す (/clone-group <prefix>)"""
    path = _channel_file(channel_id)
    if not path.exists():
        return f"context なし: {channel_id[:8]}..."
    try:
        return path.read_text(encoding="utf-8")
    except Exception:
        return f"context 読込失敗: {channel_id[:8]}..."


def forget(channel_id: str) -> bool:
    """context ファイルを削除 (/clone-group-forget)"""
    path = _channel_file(channel_id)
    if path.exists():
        path.unlink()
        return True
    return False


def find_channels(prefix: str) -> list[str]:
    """channel_id prefix に該当する channel_id を返す (海山が短い prefix で指定するため)"""
    _ensure_dir()
    if not prefix:
        return []
    matches = []
    for f in GROUP_CONTEXT_DIR.glob("*.md"):
        if f.stem.startswith(prefix):
            matches.append(f.stem)
    return matches
