#!/usr/bin/env python3
"""Google Drive folder を .gdrive_sources.json に追加 (週次 cron 取り込み対象に追加)。

Usage:
  python3 scripts/add_gdrive_source.py <folder_id_or_url> <label> [--recursive true|false]
  例:
    python3 scripts/add_gdrive_source.py 1u6vM0Ego9CNtVwuizS9jw2kjbiMpU7R7 monday-dash-weekly
    python3 scripts/add_gdrive_source.py "https://drive.google.com/drive/folders/1u6v..." monday-dash-weekly

冪等: 同じ folder_id があれば SKIP (label のみ更新)。
"""
import json
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
CONFIG = ROOT / "data" / "brain" / ".gdrive_sources.json"


def parse_folder_id(arg: str) -> str:
    """URL or ID から folder_id を抽出。"""
    m = re.search(r"/folders/([a-zA-Z0-9_-]+)", arg)
    if m:
        return m.group(1)
    # raw id 想定 (英数字 + _ - のみ)
    if re.match(r"^[a-zA-Z0-9_-]+$", arg):
        return arg
    raise ValueError(f"folder_id extract failed from: {arg}")


def main():
    args = sys.argv[1:]
    if len(args) < 2:
        print("Usage: add_gdrive_source.py <folder_id_or_url> <label> [--recursive true|false]")
        sys.exit(1)

    folder_id = parse_folder_id(args[0])
    label = args[1]
    recursive = True
    if "--recursive" in args:
        idx = args.index("--recursive")
        recursive = args[idx + 1].lower() != "false"

    # load existing
    sources = []
    if CONFIG.exists():
        sources = json.loads(CONFIG.read_text(encoding="utf-8"))
    else:
        CONFIG.parent.mkdir(parents=True, exist_ok=True)

    # dedup by folder_id
    existing_idx = next((i for i, s in enumerate(sources) if s.get("folder_id") == folder_id), None)
    new_entry = {
        "folder_id": folder_id,
        "label": label,
        "visibility": "public",
        "recursive": recursive,
    }
    if existing_idx is not None:
        old = sources[existing_idx]
        if old == new_entry:
            print(f"SKIP: already registered (folder_id={folder_id}, label={label})")
            return
        sources[existing_idx] = new_entry
        print(f"UPDATED: {folder_id} → label={label}")
    else:
        sources.append(new_entry)
        print(f"ADDED:   {folder_id} → label={label}")

    CONFIG.write_text(json.dumps(sources, indent=2, ensure_ascii=False), encoding="utf-8")
    print(f"Config: {CONFIG}")
    print(f"Total sources: {len(sources)}")
    print()
    print("次のステップ (即時取り込み):")
    print(f"  python3 gdrive_sync.py --folder {folder_id} --label {label}")
    print("または日曜 11:00 cron で自動取り込み。")


if __name__ == "__main__":
    main()
