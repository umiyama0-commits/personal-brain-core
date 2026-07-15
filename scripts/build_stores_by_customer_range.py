"""build_stores_by_customer_range.py — 日本店舗を 客数 range 別に index 化 (★2026-05-25)

# 役割

`owndays-history-stores.md` (= 1.3MB、月別 × 全店舗) は retrieval で範囲 filter 困難。
「日本で月間 350 客くらいの店」 query が hit しない真因。

→ 直近 2 ヶ月の Japan 店舗 (= JPY currency) を **客数 bucket 別** に再構成、
   `owndays-stores-by-customer-range.md` として出力。core 常駐で「N 客の店」 query に即応答可。

# bucket

  0-50 客、50-100、100-200、200-300、**300-400**、400-500、500-700、700-1000、1000+

# 出力 ~40-60KB → core truncate 12K で 1-2 月分 cover

# usage

  python3 scripts/build_stores_by_customer_range.py
  # → /app/data/brain/wiki/knowledge/owndays-stores-by-customer-range.md
"""
from __future__ import annotations

import os
import re
import sys
from collections import defaultdict
from pathlib import Path

BRAIN_ROOT = Path(os.getenv("BRAIN_ROOT", "/app/data/brain"))
WIKI = BRAIN_ROOT / "wiki" / "knowledge"
SRC = WIKI / "owndays-history-stores.md"
OUT = WIKI / "owndays-stores-by-customer-range.md"

BUCKETS: list[tuple[int, int, str]] = [
    (1000, 10_000_000, "1000 客以上"),
    (700, 1000, "700-1000 客"),
    (500, 700, "500-700 客"),
    (400, 500, "400-500 客"),
    (300, 400, "300-400 客"),
    (200, 300, "200-300 客"),
    (100, 200, "100-200 客"),
    (50, 100, "50-100 客"),
    (0, 50, "0-50 客"),
]


def parse_history_stores() -> dict[str, list[tuple[str, int, int]]]:
    """history-stores.md を月別 × 店舗 (JPY のみ) で parse.

    Returns: {month: [(name, customers, sales), ...]}
    """
    if not SRC.exists():
        print(f"ERROR: source not found: {SRC}", file=sys.stderr)
        sys.exit(2)
    content = SRC.read_text(encoding="utf-8")

    sections = re.split(r"^(## \d{4}-\d{2}\s*\n)", content, flags=re.MULTILINE)
    months_data: dict[str, list[tuple[str, int, int]]] = {}

    row_re = re.compile(
        r"^\|\s*\d+\s*\|\s*\d+\s*\|\s*(.+?)\s*\|\s*([\d,]+)\s*\|\s*([\d,]+)\s*\|\s*JPY\s*\|"
    )

    for i in range(1, len(sections), 2):
        heading = sections[i].strip()
        m = re.match(r"## (\d{4}-\d{2})", heading)
        if not m:
            continue
        month = m.group(1)
        body = sections[i + 1] if i + 1 < len(sections) else ""

        stores: list[tuple[str, int, int]] = []
        for line in body.splitlines():
            m_line = row_re.match(line)
            if not m_line:
                continue
            name = m_line.group(1).strip()
            try:
                customers = int(m_line.group(2).replace(",", ""))
                sales = int(m_line.group(3).replace(",", ""))
            except Exception:
                continue
            stores.append((name, customers, sales))

        if stores:
            months_data[month] = stores

    return months_data


def render_bucket_index(months_data: dict[str, list[tuple[str, int, int]]],
                       n_recent: int = 2) -> str:
    """直近 n_recent 月分を客数 bucket 別に整形."""
    sorted_months = sorted(months_data.keys(), reverse=True)
    target_months = sorted_months[:n_recent]

    parts = [
        "---",
        f"updated: {sorted_months[0] if sorted_months else '?'}",
        "confidence: high",
        "tags: [OWNDAYS, 売上, 客数, 店舗, range, retrieval-index]",
        "sources: [owndays-history-stores.md]",
        "clone_visibility: public",
        "---",
        "# OWNDAYS 日本店舗 客数 range 別 index (= 自動生成)",
        "",
        "**用途**: 「日本で月間 350 客くらいの店」 等の **数値 range filter query** に応答する",
        "ための pre-computed index。`owndays-history-stores.md` (= 1.3MB) は retrieval で",
        "範囲 filter 困難、ここに月別 × 客数 bucket 別 list を再構成。",
        "",
        "**再生成**: `python3 scripts/build_stores_by_customer_range.py` (= daily cron 想定)",
        "",
    ]

    for month in target_months:
        stores = months_data[month]
        parts.append(f"## {month} 月の店舗 (= 日本店舗 {len(stores)} 件、客数 range 別)")
        parts.append("")

        buckets_for_month: dict[str, list[tuple[str, int, int]]] = defaultdict(list)
        for name, customers, sales in stores:
            for lo, hi, label in BUCKETS:
                if lo <= customers < hi:
                    buckets_for_month[label].append((name, customers, sales))
                    break

        for lo, hi, label in BUCKETS:
            in_bucket = buckets_for_month.get(label, [])
            if not in_bucket:
                continue
            in_bucket.sort(key=lambda x: -x[1])  # 客数 desc
            parts.append(f"### 月間 {label} ({len(in_bucket)} 店)")
            for name, customers, sales in in_bucket:
                parts.append(f"- {name}: 客数 {customers:,} / 売上 {sales:,}円")
            parts.append("")

    parts.append("---")
    parts.append("自動生成: scripts/build_stores_by_customer_range.py")
    return "\n".join(parts)


def main():
    months_data = parse_history_stores()
    if not months_data:
        print("ERROR: no monthly data parsed", file=sys.stderr)
        sys.exit(3)

    output = render_bucket_index(months_data, n_recent=2)
    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(output, encoding="utf-8")
    n_lines = output.count("\n")
    print(f"OK: {OUT} ({OUT.stat().st_size:,} bytes, {n_lines} lines)")


if __name__ == "__main__":
    main()
