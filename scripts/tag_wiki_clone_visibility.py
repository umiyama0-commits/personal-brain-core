#!/usr/bin/env python3
"""Wiki 各ファイルに clone_visibility: public|private を付与する。

うみやまAI (LINE Works クローン) 用の Wiki 公開フィルタ。
デフォルトは PRIVATE (fail-safe)。OWNDAYS 業務 / 一般知識のみ PUBLIC に昇格。

Rules:
- Root (identity/style/thinking/index) → public
- people/*                            → private (全社員情報)
- decisions/*                         → private (HR/個別対応が中心)
    exception: press-release, summit-announcement 系 → public
- knowledge/*                         → public (OWNDAYS 業務知識)
    exception: hr-, payroll-, harassment-, tax-audit → private
- projects/*                          → owndays-*/成長 PJ は public、投資案件は private
    keyword-based heuristic で分類

Usage:
  python3 scripts/tag_wiki_clone_visibility.py --dry-run
  python3 scripts/tag_wiki_clone_visibility.py --apply
  python3 scripts/tag_wiki_clone_visibility.py --report > report.md
"""
from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path
from typing import Literal

WIKI_DIR = Path(__file__).resolve().parent.parent / "data" / "brain" / "wiki"

Visibility = Literal["public", "private"]


# ─── ルール定義 ───────────────────────────────────
ROOT_PUBLIC = {"identity.md", "style.md", "thinking.md"}
# index.md は全 Wiki ファイルへの参照を含む TOC。public にすると private ファイル名が
# クローン応答経由で露出するので private 扱いにする。

# decisions/ のホワイトリスト (発表済み方針)
DECISIONS_PUBLIC_PATTERNS = [
    "press-release",
    "summit-announcement",
    "summit-announcements",
]

# knowledge/ のブラックリスト (個別社員情報 / 機密)
KNOWLEDGE_PRIVATE_PATTERNS = [
    "hr-metrics",
    "hr-operations",
    "payroll",
    "harassment",
    "tax-audit",
]

# projects/ の投資系 (private)
PROJECTS_INVESTMENT_PATTERNS = [
    "invest",  # flexii-investment, any-investment
    "garden",  # example-garden
    "angel",
    "example",
    "flexii",
    "fund",
    "portfolio",
]

# projects/ の OWNDAYS プロジェクトホワイトリスト (public)
PROJECTS_OWNDAYS_PATTERNS = [
    "owndays",
    "op-",
    "store-",
    "campaign",
    "brand",
    "summit",
    "ceremony",
    "lens",
    "collection",
    "collaboration",
    "marketing",
    "crm",
    "cvr",
    "eyewear",
    "glasses",
]


def classify(file_path: Path) -> tuple[Visibility, str]:
    """返り値: (public|private, 判定理由)"""
    rel = file_path.relative_to(WIKI_DIR)
    name = rel.name.lower()

    # Root
    if len(rel.parts) == 1:
        if rel.name in ROOT_PUBLIC:
            return "public", "root alignment file"
        return "private", "unknown root file → default private"

    top = rel.parts[0]

    # people/
    if top == "people":
        return "private", "individual employee information"

    # decisions/
    if top == "decisions":
        if any(k in name for k in DECISIONS_PUBLIC_PATTERNS):
            return "public", "published announcement / press release"
        return "private", "internal decision record"

    # knowledge/
    if top == "knowledge":
        if any(k in name for k in KNOWLEDGE_PRIVATE_PATTERNS):
            return "private", "HR / sensitive domain knowledge"
        return "public", "general OWNDAYS business knowledge"

    # projects/
    if top == "projects":
        if any(k in name for k in PROJECTS_INVESTMENT_PATTERNS):
            return "private", "personal investment project"
        if any(k in name for k in PROJECTS_OWNDAYS_PATTERNS):
            return "public", "OWNDAYS core project"
        # ambiguous projects default → private (fail-safe)
        return "private", "ambiguous project → default private"

    return "private", "unclassified → default private"


# ─── frontmatter 操作 ───────────────────────────────
FRONTMATTER_RE = re.compile(r"^---\n(.*?)\n---\n", re.DOTALL)


def read_current_visibility(content: str) -> str | None:
    m = FRONTMATTER_RE.match(content)
    if not m:
        return None
    fm = m.group(1)
    for line in fm.splitlines():
        if line.startswith("clone_visibility:"):
            return line.split(":", 1)[1].strip()
    return None


def update_frontmatter(content: str, visibility: Visibility) -> str:
    """先頭の --- ブロック内に clone_visibility を追加/更新"""
    m = FRONTMATTER_RE.match(content)
    new_line = f"clone_visibility: {visibility}"

    if not m:
        # frontmatter 無し → 新規作成
        return f"---\n{new_line}\n---\n{content}"

    fm = m.group(1)
    lines = fm.splitlines()
    replaced = False
    for i, line in enumerate(lines):
        if line.startswith("clone_visibility:"):
            lines[i] = new_line
            replaced = True
            break
    if not replaced:
        lines.append(new_line)

    new_fm = "\n".join(lines)
    rest = content[m.end():]
    return f"---\n{new_fm}\n---\n{rest}"


# ─── メイン処理 ───────────────────────────────────
def scan_wiki():
    files = sorted(WIKI_DIR.rglob("*.md"))
    return files


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true", help="変更を表示するだけで書き込まない")
    ap.add_argument("--apply", action="store_true", help="実際に frontmatter に書き込む")
    ap.add_argument("--report", action="store_true", help="Markdown 形式の分類レポートを出力")
    ap.add_argument("--only-changes", action="store_true", help="新規 + 変更のみ表示")
    args = ap.parse_args()

    if not (args.dry_run or args.apply or args.report):
        args.dry_run = True  # default

    files = scan_wiki()
    results = []

    for fp in files:
        try:
            content = fp.read_text(encoding="utf-8")
        except Exception as e:
            print(f"[ERROR] {fp}: {e}", file=sys.stderr)
            continue

        current = read_current_visibility(content)
        new_vis, reason = classify(fp)
        changed = current != new_vis

        results.append({
            "path": fp.relative_to(WIKI_DIR),
            "current": current,
            "new": new_vis,
            "reason": reason,
            "changed": changed,
        })

        if args.apply and changed:
            new_content = update_frontmatter(content, new_vis)
            fp.write_text(new_content, encoding="utf-8")

    # ─── 出力 ───
    if args.report:
        print_report(results)
    else:
        print_summary(results, only_changes=args.only_changes)


def print_summary(results, only_changes=False):
    pub = sum(1 for r in results if r["new"] == "public")
    prv = sum(1 for r in results if r["new"] == "private")
    changes = sum(1 for r in results if r["changed"])
    print(f"Total: {len(results)} | PUBLIC: {pub} | PRIVATE: {prv} | Changes: {changes}")
    print("")
    rows = [r for r in results if r["changed"]] if only_changes else results
    for r in rows:
        mark = "→" if r["changed"] else " "
        cur = r["current"] or "none"
        print(f"  [{r['new']:7}] {mark} ({cur:7} → {r['new']:7}) {r['path']}  # {r['reason']}")


def print_report(results):
    print("# うみやまAI Wiki 公開分類レポート")
    print(f"Total: {len(results)}")
    pub = [r for r in results if r["new"] == "public"]
    prv = [r for r in results if r["new"] == "private"]
    print(f"- PUBLIC: {len(pub)}")
    print(f"- PRIVATE: {len(prv)}")
    print()

    print("## PUBLIC (うみやまAI が参照可能)")
    for r in pub:
        print(f"- `{r['path']}` — {r['reason']}")

    print("\n## PRIVATE (うみやまAI から隠蔽)")
    # Group by top directory
    by_dir = {}
    for r in prv:
        top = r["path"].parts[0] if len(r["path"].parts) > 1 else "root"
        by_dir.setdefault(top, []).append(r)

    for d, items in sorted(by_dir.items()):
        print(f"\n### {d}/ ({len(items)})")
        for r in items:
            print(f"- `{r['path']}` — {r['reason']}")


if __name__ == "__main__":
    main()
