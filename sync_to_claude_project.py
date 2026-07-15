"""
sync_to_claude_project.py — Brain Wiki → Claude.ai プロジェクト同期

Brain Wiki + 当日のカレンダー/メールを1つのナレッジファイルにまとめ、
Claude.ai の Project Knowledge として使えるようにする。

使い方:
  python3 sync_to_claude_project.py              # ファイル生成のみ
  python3 sync_to_claude_project.py --upload      # API経由でアップロード

生成ファイル: data/brain/claude_project_knowledge.md
→ Claude.ai のプロジェクトに手動 or API でアップロード
"""

import argparse
import json
import logging
import os
import sys
from datetime import datetime, date
from pathlib import Path

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s")
logger = logging.getLogger(__name__)

BRAIN_ROOT = Path("/Users/brain/brain-agent/data/brain")
WIKI_DIR = BRAIN_ROOT / "wiki"
OUTPUT_FILE = BRAIN_ROOT / "claude_project_knowledge.md"

sys.path.insert(0, "/Users/brain/brain-agent")


def gather_wiki() -> str:
    """Wiki全ページを結合"""
    sections = []
    if not WIKI_DIR.exists():
        return ""

    from brain_wiki_helpers.domain import is_owndays_facing, is_personal_rel
    # ★2026-07-03 (R6 cross-check DA 4b): 従来この export には interview/ 全文が入っており、
    #   海山が Claude.ai project で深層を能動的に使っていた可能性がある。default は除外
    #   (export md は disk 上の平文 = コピー/共有され得る) だが、海山判断で戻せるよう opt-in。
    include_interview = os.getenv("CLAUDE_SYNC_INCLUDE_INTERVIEW", "0") == "1"
    for f in sorted(WIKI_DIR.rglob("*.md")):
        rel = f.relative_to(WIKI_DIR)
        # ★2026-06-28 personal / ★2026-07-03 interview (v3 ADR DA R6): Claude.ai project export に
        #   深層 private (非OWNDAYS PJ + 人格深層) を含めない — 外部 upload の最重要 chokepoint
        if include_interview:
            if is_personal_rel(rel):
                continue
        elif not is_owndays_facing(rel):
            continue
        content = f.read_text(encoding="utf-8").strip()
        if content:
            sections.append(f"## Wiki: {rel}\n\n{content}")

    return "\n\n---\n\n".join(sections)


def gather_calendar() -> str:
    """今日+明日の予定"""
    try:
        from google_sync import get_credentials, sync_calendar
        creds = get_credentials()
        events = sync_calendar(creds, days=2, dry_run=True)
        if not events:
            return "予定なし"

        lines = []
        for ev in events:
            t = ev["start"][11:16] if "T" in ev["start"] else "終日"
            d = ev["start"][:10]
            line = f"- [{d} {t}] {ev['summary']}"
            if ev.get("attendees"):
                line += f" ({', '.join(ev['attendees'][:4])})"
            lines.append(line)
        return "\n".join(lines)
    except Exception as e:
        logger.warning(f"Calendar error: {e}")
        return f"(取得エラー: {e})"


def gather_mail() -> str:
    """直近1日のメール"""
    try:
        from google_sync import get_credentials, sync_gmail
        creds = get_credentials()
        emails = sync_gmail(creds, days=1, max_emails=15, dry_run=True)
        if not emails:
            return "新着なし"

        lines = []
        for em in emails:
            sender = em["from"].split("<")[0].strip().strip('"')[:25]
            lines.append(f"- {sender} | {em['subject'][:60]}")
        return "\n".join(lines)
    except Exception as e:
        logger.warning(f"Mail error: {e}")
        return f"(取得エラー: {e})"


def gather_recent_conversations(days: int = 3) -> str:
    """直近の会話ログ要約"""
    conv_dir = BRAIN_ROOT / "raw" / "conversations"
    if not conv_dir.exists():
        return ""

    files = sorted(conv_dir.glob("*.md"), reverse=True)[:days]
    sections = []
    for f in files:
        content = f.read_text(encoding="utf-8").strip()
        if content:
            # 長すぎる場合は切り詰め
            if len(content) > 3000:
                content = content[:3000] + "\n...(省略)"
            sections.append(f"### {f.stem}\n{content}")

    return "\n\n".join(sections)


def build_knowledge_file() -> str:
    """全データを1つのナレッジファイルに結合"""
    now = datetime.now().strftime("%Y-%m-%d %H:%M")

    wiki = gather_wiki()
    calendar = gather_calendar()
    mail = gather_mail()
    conversations = gather_recent_conversations()

    doc = f"""# Personal Brain — OWNDAYS CEO 海山丈司
最終更新: {now}

このファイルはPersonal Brain AIシステムから自動生成されたナレッジベースです。
以下の情報に基づいて、海山丈司（OWNDAYS CEO）のパーソナルAIアシスタントとして振る舞ってください。

---

# 1. Brain Wiki（知識ベース）

{wiki}

---

# 2. 本日のスケジュール

{calendar}

---

# 3. 直近のメール

{mail}

---

# 4. 直近の会話ログ

{conversations}

---

# 利用ガイドライン

- このデータに基づいて具体的に回答してください
- 数値や固有名詞は正確に引用してください
- データにない情報は「Brain Wikiに該当情報がありません」と回答してください
- 「AIなのでできません」等の回答は禁止です
"""
    return doc


def upload_to_project(api_key: str, project_id: str, content: str):
    """Claude API経由でプロジェクトにアップロード"""
    import httpx

    # ファイルをプロジェクトknowledgeとして追加
    resp = httpx.post(
        f"https://api.anthropic.com/v1/projects/{project_id}/docs",
        headers={
            "x-api-key": api_key,
            "anthropic-version": "2024-10-01",
            "content-type": "application/json",
        },
        json={
            "name": "brain_knowledge",
            "content": content,
        },
        timeout=30.0,
    )
    if resp.status_code in (200, 201):
        logger.info(f"アップロード成功: project={project_id}")
    else:
        logger.error(f"アップロード失敗: {resp.status_code} {resp.text}")


def main():
    parser = argparse.ArgumentParser(description="Brain Wiki → Claude Project 同期")
    parser.add_argument("--upload", action="store_true", help="APIでアップロード")
    parser.add_argument("--api-key", default="", help="Anthropic API Key")
    parser.add_argument("--project-id", default="", help="Claude Project ID")
    args = parser.parse_args()

    logger.info("ナレッジファイル生成中...")
    content = build_knowledge_file()

    # ファイル保存
    OUTPUT_FILE.parent.mkdir(parents=True, exist_ok=True)
    OUTPUT_FILE.write_text(content, encoding="utf-8")
    logger.info(f"保存: {OUTPUT_FILE} ({len(content)} 文字)")

    if args.upload and args.api_key and args.project_id:
        upload_to_project(args.api_key, args.project_id, content)
    else:
        logger.info("Claude.ai → プロジェクト → 左サイドバー「Knowledge」にこのファイルをドラッグ&ドロップしてください")
        logger.info(f"ファイル: {OUTPUT_FILE}")


if __name__ == "__main__":
    main()
