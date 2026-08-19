"""
clone_reminder_check.py — 1 回限りの reminder を当日に LINE Push (★2026-05-23 海山指示)

設計:
  schedule skill (Anthropic cloud remote agent) は LINE_CHANNEL_ACCESS_TOKEN を
  secret 漏洩リスクで持てない (MCP connector 不在)。代わりに Personal Brain の
  既存 cron に「reminder check」を組み込み、host から line_push で配信。

使い方:
  1. data/brain/reminders/<YYYY-MM-DD>.md に reminder 本文を書く
     (1 行目を Push title に使う、本文全文を LINE Push 本文に)
  2. 09:00 JST daily で動く clone_cron.sh cost-daily の冒頭で本 script が走る
  3. 当日のファイルがあれば line_push、送信後 reminders/_sent/ に move (重複防止)

実行:
  python3 scripts/clone_reminder_check.py             # 当日の reminder を check
  python3 scripts/clone_reminder_check.py --dry-run   # Push しない、内容だけ表示
  python3 scripts/clone_reminder_check.py --date 2026-05-25   # 日付指定 (debug 用)
"""
from __future__ import annotations

import argparse
import logging
import os
import shutil
import sys
from datetime import datetime, timezone, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from clone_improve_lib import line_push  # noqa: E402

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger("clone_reminder_check")

JST = timezone(timedelta(hours=9))
APP_ROOT = Path(os.getenv("BRAIN_APP_ROOT", "/app"))
REMINDERS_DIR = APP_ROOT / "data" / "brain" / "reminders"
SENT_DIR = REMINDERS_DIR / "_sent"


def find_reminder(date_str: str) -> list[Path]:
    """当日の reminder file 群。手動 (<date>.md) + bot 自動生成 (auto/<date>.md、
    ★2026-07-20 owner_memory.create_reminder = git 非追跡 subdir に分離)。"""
    out = []
    for path in (REMINDERS_DIR / f"{date_str}.md", REMINDERS_DIR / "auto" / f"{date_str}.md"):
        if path.exists() and path.is_file():
            out.append(path)
    return out


def push_reminder(reminder_path: Path, dry_run: bool = False) -> bool:
    """reminder の内容を LINE Push、成功したら _sent/ に move。"""
    try:
        content = reminder_path.read_text(encoding="utf-8")
    except Exception as e:
        logger.warning(f"reminder read failed: {reminder_path}: {e}")
        return False

    # title を 1 行目から抽出
    lines = content.splitlines()
    title = "📅 リマインド"
    body = content
    if lines and lines[0].strip().startswith("#"):
        title = lines[0].lstrip("# ").strip() or title
        body = "\n".join(lines[1:]).strip()

    push_text = f"{title}\n\n{body}"
    # LINE 1 message 上限 ~5000 字
    if len(push_text) > 4500:
        push_text = push_text[:4400] + "\n\n... (truncated)"

    if dry_run:
        logger.info(f"[dry-run] would push ({len(push_text)} chars):\n{push_text[:300]}...")
        return True

    ok = line_push(push_text, critical=True)  # ★2026-08-03: ADR 2026-07-20 が即時と定めた系統 (日次上限に当たっても遅延させない)
    if ok:
        # _sent/ に move (重複送信防止)
        SENT_DIR.mkdir(parents=True, exist_ok=True)
        ts = datetime.now(JST).strftime("%Y-%m-%d_%H%M%S")
        dest = SENT_DIR / f"{reminder_path.stem}_sent_{ts}.md"
        try:
            shutil.move(str(reminder_path), str(dest))
            logger.info(f"reminder pushed and moved to {dest.name}")
        except Exception as e:
            logger.warning(f"move failed (push succeeded but file left in place): {e}")
        return True
    logger.warning("LINE Push failed (line_push returned False)")
    return False


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--date", default=None, help="YYYY-MM-DD (default: 今日 JST)")
    parser.add_argument("--dry-run", action="store_true", help="Push しない、内容だけ表示")
    args = parser.parse_args()

    today = args.date or datetime.now(JST).strftime("%Y-%m-%d")
    reminders = find_reminder(today)
    if not reminders:
        logger.info(f"no reminder for {today} (looked at {REMINDERS_DIR}/[auto/]{today}.md)")
        return 0

    # 全件 push (list 化で all() の短絡による取り残しを防ぐ)
    results = [push_reminder(p, dry_run=args.dry_run) for p in reminders]
    for p in reminders:
        logger.info(f"found reminder: {p}")
    return 0 if all(results) else 1


if __name__ == "__main__":
    sys.exit(main())
