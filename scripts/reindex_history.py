"""
reindex_history.py — docker container 内で実行して
owndays-history-*.md を Chroma に index する。

使い方 (host から):
  docker exec line-bot python3 /app/scripts/reindex_history.py

または:
  docker exec line-bot python3 /app/scripts/reindex_history.py --all  # 全 wiki 再索引
"""
from __future__ import annotations

import argparse
import asyncio
import logging
import os
import sys
from pathlib import Path

# /app を sys.path に (docker 内)
sys.path.insert(0, "/app")
if os.path.exists("/Users/brain/brain-agent"):
    sys.path.insert(0, "/Users/brain/brain-agent")  # host 直実行用 (開発)

import httpx

from brain_index import BrainIndex

logger = logging.getLogger(__name__)
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s: %(message)s")

LITELLM_URL = os.getenv("LITELLM_URL", "http://litellm:4000")
LITELLM_KEY = os.getenv("LITELLM_MASTER_KEY", "")

# WIKI_DIR: docker 内なら /app/data/... host なら ./data/...
WIKI_DIR_CANDIDATES = [
    Path("/app/data/brain/wiki"),
    Path("/Users/brain/brain-agent/data/brain/wiki"),
]


def find_wiki_dir() -> Path:
    for p in WIKI_DIR_CANDIDATES:
        if p.exists():
            return p
    raise RuntimeError(f"Wiki dir not found: {WIKI_DIR_CANDIDATES}")


async def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--all", action="store_true", help="全 wiki 再索引")
    parser.add_argument(
        "--glob",
        default="knowledge/owndays-history-*.md",
        help="対象ファイル glob (default: knowledge/owndays-history-*.md)",
    )
    args = parser.parse_args()

    wiki_dir = find_wiki_dir()
    logger.info(f"wiki_dir: {wiki_dir}")

    async with httpx.AsyncClient(timeout=60) as http:
        idx = BrainIndex(http, LITELLM_URL, LITELLM_KEY)

        if args.all:
            logger.info("Full wiki reindex...")
            await idx.reindex_all_wiki(wiki_dir)
        else:
            targets = sorted(wiki_dir.glob(args.glob))
            if not targets:
                # 通常 daily も再索引対象に含めたい
                targets = sorted(wiki_dir.glob(args.glob)) + sorted(
                    wiki_dir.glob("knowledge/owndays-daily-*.md")
                )
            logger.info(f"Reindexing {len(targets)} files (glob={args.glob}):")
            for md in targets:
                logger.info(f"  - {md.relative_to(wiki_dir)}")
                try:
                    await idx.index_wiki_file(md)
                except Exception as e:
                    logger.error(f"    failed: {e}")

        stats = idx.get_stats()
        logger.info(f"index stats: {stats}")

    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
