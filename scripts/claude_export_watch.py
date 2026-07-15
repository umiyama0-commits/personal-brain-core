#!/usr/bin/env python3
"""scripts/claude_export_watch.py — Claude.ai export zip を監視フォルダから自動取込(backstop)。

★2026-06-29 海山指示「Example PJ の全会話を自動取込(両方=scrape主+export補完)」。
海山が Claude.ai を export → 監視フォルダ(~/Downloads + data/brain/import_exports/)に zip を置くだけで:
  ① claude_export_import  : Example Garden PJ の全会話 → wiki/personal/example-garden/(PJ単位・private)
  ② claude_export_alignment: アラインメント雑談 → 人格蒸留(レビュー待ち)
の両方へ自動で流す。処理済 zip は state で記録(再処理しない)。日次スクレイプが取りこぼした/セッション切れ
の時の確実な補完。scrape と重複しても write_personal_abstract の conv_id dedup で二重取込されない。

実行(host cron、daily): python3 scripts/claude_export_watch.py [--dry-run]
"""
from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import logging
import os
import sys
import zipfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))         # scripts/ sibling

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger("claude_export_watch")

ROOT = Path(__file__).resolve().parents[1]
STATE_FILE = ROOT / "data" / "brain" / ".export_watch_state.json"
WATCH_DIRS = [
    Path(os.path.expanduser("~/Downloads")),
    ROOT / "data" / "brain" / "import_exports",
]


def _is_claude_export(z: Path) -> bool:
    """zip 内に conversations.json があれば Claude.ai export とみなす。"""
    try:
        if not zipfile.is_zipfile(z):
            return False
        with zipfile.ZipFile(z) as zf:
            return any(n.endswith("conversations.json") for n in zf.namelist())
    except Exception:
        return False


def _digest(z: Path) -> str:
    """name + size で軽量に同一性判定(中身 hash は重いので避ける)。"""
    try:
        return hashlib.sha1(f"{z.name}:{z.stat().st_size}".encode()).hexdigest()[:16]
    except OSError:
        return hashlib.sha1(z.name.encode()).hexdigest()[:16]


def _load_done() -> set:
    if STATE_FILE.exists():
        try:
            return set(json.loads(STATE_FILE.read_text(encoding="utf-8")).get("done", []))
        except Exception:
            pass
    return set()


def _save_done(done: set) -> None:
    STATE_FILE.write_text(json.dumps({"done": list(done)[-200:]}, ensure_ascii=False, indent=2),
                          encoding="utf-8")


def find_exports(dirs=None) -> list[Path]:
    dirs = dirs or WATCH_DIRS
    found: list[Path] = []
    for d in dirs:
        if d.exists():
            found += [z for z in sorted(d.glob("*.zip")) if _is_claude_export(z)]
    return found


async def run(*, dry_run: bool = False, dirs=None, importers=None) -> dict:
    done = _load_done()
    fresh = [z for z in find_exports(dirs) if _digest(z) not in done]
    logger.info(f"export zip 検出のうち未処理 {len(fresh)} 件")
    if not fresh:
        return {"ok": True, "processed": 0, "note": "新規 export 無し"}

    if importers is None:                                # 遅延 import(CI 軽量・テストは注入)
        import claude_export_import as cei
        import claude_export_alignment as cea
        importers = (cei.run, cea.run)
    example_run, align_run = importers

    processed = 0
    for z in fresh:
        if dry_run:
            logger.info(f"  [dry] {z.name}")
            continue
        try:
            r1 = await example_run(z)                     # Example → personal
            r2 = await align_run(z)                       # アラインメント → 人格
            logger.info(f"  取込: {z.name} example={r1} align={r2}")
            done.add(_digest(z))
            processed += 1
        except Exception as e:
            logger.warning(f"  失敗 {z.name}: {type(e).__name__}: {e}")
    if not dry_run and processed:
        _save_done(done)
    return {"ok": True, "processed": processed, "found": len(fresh)}


def main() -> int:
    ap = argparse.ArgumentParser(description="Claude.ai export zip を監視フォルダから自動取込(backstop)")
    ap.add_argument("--dry-run", action="store_true", help="検出だけ表示(取込まない)")
    a = ap.parse_args()
    r = asyncio.run(run(dry_run=a.dry_run))
    print(r)
    return 0 if r.get("ok") else 1


if __name__ == "__main__":
    raise SystemExit(main())
