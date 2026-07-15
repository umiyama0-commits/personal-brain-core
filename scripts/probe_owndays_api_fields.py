"""probe_owndays_api_fields.py — OWNDAYS Net Mobile API の生 JSON field を確認

★2026-05-25 (海山 質問: 「OWNDAYS net の中に昨年対比という情報はない？」)
→ scraper code は NationName/Amount/DollarShort/CustomerCount しか読んでないが、
   API は他 field (= LastYearAmount, PrevYearCustomerCount 等) を返してる可能性が高い。
   実機で /api/nationtotal を叩いて生 JSON を dump、YoY 系 field の有無を確認する。

usage (Mac Studio で):
  python3 scripts/probe_owndays_api_fields.py             # today
  python3 scripts/probe_owndays_api_fields.py 2025-05-24  # 任意 date

env required:
  OWNDAYS_MOBILE_USER / OWNDAYS_MOBILE_PASS (= .env から自動 load)
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import sys
from datetime import date
from pathlib import Path

try:
    from dotenv import load_dotenv
    load_dotenv(Path(__file__).resolve().parents[1] / ".env")
except ImportError:
    pass

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s: %(message)s")
logger = logging.getLogger(__name__)

# 既存の login + api_post を借用
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from mobile_owndays_scraper import ensure_login  # noqa: E402
from mobile_owndays_historical import api_post  # noqa: E402

# ★fix 2026-05-25 (海山指示):
# YoY 系 field の正規 identifier (probe 1 回目で実機判明 + 海山 review 反映):
#   - "yAmount"     = 前年同期 売上額 (★ローカル通貨ベース、JPY 換算前)
#   - "achievement" = 前年比 % (Amount / yAmount で OWNDAYS 側で計算済)
# 重要 note: YoY 比較は **ローカル通貨で行う** (台湾 TWD、UAE AED 等は
# JPYAmount で比較すると為替変動でノイズが乗る = 経営判断の誤り源)。
# heuristic 検出は前回 "DollarRate" (為替レート) を誤検出していたので
# 名指し allowlist + blocklist で正確化。
_YOY_FIELDS_WHITELIST = {"yAmount", "achievement"}
_YOY_KEYWORDS = ["last", "prev", "previous", "year", "yoy", "compare"]
_YOY_BLOCKLIST = {"DollarRate", "DollarShort"}  # 為替系は YoY ではない


def _detect_yoy_fields(keys):
    """field 名から YoY 関連を抽出。allowlist 優先 + keyword 補完 - blocklist。"""
    keys = list(keys)
    hits = []
    for k in keys:
        if k in _YOY_BLOCKLIST:
            continue
        if k in _YOY_FIELDS_WHITELIST:
            hits.append(k)
            continue
        if any(kw in k.lower() for kw in _YOY_KEYWORDS):
            hits.append(k)
    return hits


def _print_yoy_findings(keys):
    """YoY 系 field の発見状況を表示 + 解説。"""
    hits = _detect_yoy_fields(keys)
    if not hits:
        print("  (no YoY-related field)")
        return
    print(f"  ★ YoY 関連 field: {hits}")
    if "yAmount" in hits:
        print("    - yAmount      = 前年同期 売上額 (★ローカル通貨ベース、JPY 換算しない)")
    if "achievement" in hits:
        print("    - achievement  = 前年比 % (Amount / yAmount、OWNDAYS 側で計算済)")


async def probe(target_date: str):
    """指定日の各 API endpoint を叩いて生 JSON を dump"""
    from playwright.async_api import async_playwright

    endpoints = [
        "/api/total",
        "/api/totaldaily",
        "/api/nationtotal",
        "/api/areatotal",
        "/api/typetotal",
        "/api/leaguetotal",
        "/api/storelist",
    ]

    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)
        context = await browser.new_context()
        page = await context.new_page()

        ok = await ensure_login(page, context)
        if not ok:
            logger.error("login failed")
            return

        # 1 日範囲で各 endpoint を叩く
        payload = {"startDate": target_date, "endDate": target_date}
        for endpoint in endpoints:
            logger.info(f"\n=== POST {endpoint} {payload} ===")
            data = await api_post(page, endpoint, payload, retries=1)
            if data is None:
                logger.warning(f"  ↑ failed or empty")
                continue

            # 構造を pretty print: 配列なら 1 件目の keys、dict ならそのまま
            if isinstance(data, list):
                if not data:
                    print(f"  EMPTY LIST")
                    continue
                first = data[0]
                if isinstance(first, dict):
                    print(f"  list[{len(data)}], keys of [0]:")
                    for k, v in first.items():
                        print(f"    {k}: {repr(v)[:80]}")
                    _print_yoy_findings(first.keys())
                else:
                    print(f"  list[{len(data)}] of {type(first).__name__}, [0]={repr(first)[:200]}")
            elif isinstance(data, dict):
                print(f"  dict keys:")
                for k, v in data.items():
                    print(f"    {k}: {repr(v)[:80]}")
                _print_yoy_findings(data.keys())
            else:
                print(f"  {type(data).__name__}: {repr(data)[:300]}")

        await browser.close()


def main():
    if len(sys.argv) > 1:
        target = sys.argv[1]
        try:
            date.fromisoformat(target)
        except ValueError:
            print(f"ERROR: invalid date {target}, use YYYY-MM-DD", file=sys.stderr)
            sys.exit(2)
    else:
        target = date.today().isoformat()

    asyncio.run(probe(target))


if __name__ == "__main__":
    main()
