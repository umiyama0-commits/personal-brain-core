"""
apple_notes_sync.py — Apple メモ → Brain Wiki 同期

AppleScript経由でApple Notesの全メモを取得し、
data/brain/import/ に配置してファイルウォッチャー経由でBrainWikiに取り込む。

使い方:
  python3 apple_notes_sync.py                # 全メモを同期
  python3 apple_notes_sync.py --since 7      # 過去7日間の更新分のみ
  python3 apple_notes_sync.py --dry-run      # プレビューのみ

crontab:
  0 23 * * * /Users/brain/brain-agent/apple_notes_sync.py >> /Users/brain/brain-agent/data/brain/scrape.log 2>&1
"""

import subprocess
import json
import hashlib
import logging
import argparse
from datetime import datetime, date, timedelta
from pathlib import Path

logger = logging.getLogger(__name__)

OUTPUT_DIR = Path("/Users/brain/brain-agent/data/brain/import")
STATE_FILE = Path("/Users/brain/brain-agent/data/brain/.apple_notes_state.json")


def get_all_notes(since_days: int = 0) -> list[dict]:
    """AppleScript経由でメモを取得

    since_days > 0: 過去N日間に更新されたメモのみ (タイムアウト回避)
    since_days = 0: 全メモ (初回 or 強制リフレッシュ用)
    """
    if since_days > 0:
        # 日付フィルタを AppleScript 内で適用 (大量ノート時のタイムアウト回避)
        date_filter = f'''
        set cutoffDate to (current date) - ({since_days} * days)
        repeat with n in (every note whose modification date > cutoffDate)
        '''
    else:
        date_filter = "repeat with n in every note"

    script = f'''
    tell application "Notes"
        set output to ""
        {date_filter}
            try
                set noteName to name of n
                set noteBody to plaintext of n
                set noteDate to modification date of n
                set noteId to id of n
                try
                    set noteFolder to name of container of n
                on error
                    set noteFolder to "Notes"
                end try
                set output to output & "<<NOTE_START>>" & noteId & "<<SEP>>" & noteName & "<<SEP>>" & noteFolder & "<<SEP>>" & (noteDate as string) & "<<SEP>>" & noteBody & "<<NOTE_END>>"
            end try
        end repeat
        return output
    end tell
    '''
    # since_days > 0 でも 14日 = 数百ノート見る可能性あり → 180s
    # 全件は 600s (10分)
    timeout_sec = 180 if since_days > 0 else 600
    try:
        result = subprocess.run(
            ["osascript", "-e", script],
            capture_output=True, text=True, timeout=timeout_sec
        )
        if result.returncode != 0:
            logger.error(f"AppleScript error: {result.stderr}")
            return []

        raw = result.stdout
        notes = []
        for block in raw.split("<<NOTE_START>>"):
            block = block.strip()
            if not block or "<<SEP>>" not in block:
                continue
            block = block.replace("<<NOTE_END>>", "")
            parts = block.split("<<SEP>>", 4)
            if len(parts) < 5:
                continue
            note_id, name, folder, mod_date, body = parts
            notes.append({
                "id": note_id.strip(),
                "name": name.strip(),
                "folder": folder.strip(),
                "modified": mod_date.strip(),
                "body": body.strip(),
            })

        logger.info(f"Apple Notes: {len(notes)} メモ取得")
        return notes
    except subprocess.TimeoutExpired:
        logger.error("AppleScript timed out")
        return []
    except Exception as e:
        logger.error(f"Apple Notes取得エラー: {e}")
        return []


def _content_hash(text: str) -> str:
    return hashlib.md5(text.encode()).hexdigest()


def load_state() -> dict:
    """前回同期時のハッシュを読み込む"""
    if STATE_FILE.exists():
        return json.loads(STATE_FILE.read_text())
    return {}


def save_state(state: dict):
    STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
    STATE_FILE.write_text(json.dumps(state, ensure_ascii=False, indent=2))


def sync_notes(dry_run: bool = False, since_days: int = 0):
    """Apple Notesを同期。変更があったメモのみ取り込み。"""
    notes = get_all_notes(since_days=since_days)
    if not notes:
        return

    state = load_state()
    new_state = {}
    changed = []

    for note in notes:
        content_hash = _content_hash(note["body"])
        new_state[note["id"]] = content_hash

        # 変更がないメモはスキップ
        if state.get(note["id"]) == content_hash:
            continue

        # 短すぎるメモはスキップ
        if len(note["body"].strip()) < 10:
            continue

        changed.append(note)

    logger.info(f"変更/新規メモ: {len(changed)}/{len(notes)}")

    if dry_run:
        for note in changed[:20]:
            print(f"\n[{note['folder']}] {note['name']}")
            print(f"  {note['body'][:100]}...")
        print(f"\n合計: {len(changed)} メモが取り込み対象")
        return

    # エクスポートファイルとして保存
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    today = date.today().isoformat()

    # メモをバッチ化（1ファイルに最大20メモ）
    batch_size = 20
    for batch_idx in range(0, len(changed), batch_size):
        batch = changed[batch_idx:batch_idx + batch_size]
        filename = f"apple_notes_{today}_batch{batch_idx // batch_size}.txt"
        filepath = OUTPUT_DIR / filename

        lines = [f"[Apple Notes Export] {today}", ""]
        for note in batch:
            lines.append(f"## [{note['folder']}] {note['name']}")
            lines.append(f"Modified: {note['modified']}")
            lines.append("")
            lines.append(note["body"])
            lines.append("")
            lines.append("---")
            lines.append("")

        filepath.write_text("\n".join(lines), encoding="utf-8")
        logger.info(f"保存: {filepath.name} ({len(batch)} メモ)")

    # 状態を保存
    save_state(new_state)
    logger.info(f"同期完了: {len(changed)} メモ → data/brain/import/")


def main():
    parser = argparse.ArgumentParser(description="Apple Notes → Brain Wiki 同期")
    parser.add_argument("--dry-run", action="store_true", help="プレビューのみ")
    parser.add_argument(
        "--since",
        type=int,
        default=14,
        help="過去N日間に更新されたメモのみ取得 (デフォルト 14日、0 で全件)",
    )
    parser.add_argument("--force", action="store_true", help="ハッシュチェックをスキップして全件取り込み")
    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s")

    if args.force:
        # 状態ファイルを削除して全件取り込み
        STATE_FILE.unlink(missing_ok=True)

    sync_notes(dry_run=args.dry_run, since_days=args.since)


if __name__ == "__main__":
    main()
