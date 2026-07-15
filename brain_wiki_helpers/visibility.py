"""
brain_wiki_helpers/visibility.py — wiki frontmatter から visibility / retired 判定

★2026-05-22 Phase 1a 切り出し:
brain_wiki.BrainWiki._parse_clone_visibility と _parse_is_retired を pure function 化。
既存 method は薄い wrapper にして API 互換維持。
"""
from __future__ import annotations

import re
from datetime import date as _date, datetime as _dt
from typing import Optional


def parse_clone_visibility(content: str) -> str:
    """frontmatter から clone_visibility を取得。未設定は private (fail-safe)。

    例:
      ---
      clone_visibility: public
      ---
      ...
    → "public"
    """
    m = re.match(r"^---\n(.*?)\n---\n", content, re.DOTALL)
    if not m:
        return "private"
    for line in m.group(1).splitlines():
        if line.startswith("clone_visibility:"):
            return line.split(":", 1)[1].strip()
    return "private"


def parse_is_retired(content: str) -> bool:
    """frontmatter から bi-temporal 廃止状態を判定 (★2026-05-21 項目 9)。

    次のいずれかが立っていれば retired (= うみやまAI が retrieval で踏まない):
    - `superseded_by: <id>` (後継パターンが明示されている)
    - `valid_until: YYYY-MM-DD` (今日以前)

    retired ファイルは記録としては残るが、AI 応答の文脈には入れない。
    判断パターン (judgment) / 反射 (reflex) / 言語パターン (style) の
    過去版が「腐ったまま」応答に混入するのを防ぐ。
    """
    m = re.match(r"^---\n(.*?)\n---\n", content, re.DOTALL)
    if not m:
        return False
    fm_text = m.group(1)
    for line in fm_text.splitlines():
        line_s = line.strip()
        if line_s.startswith("superseded_by:"):
            val = line.split(":", 1)[1].strip().strip('"').strip("'")
            if val:
                return True
        elif line_s.startswith("valid_until:"):
            val = line.split(":", 1)[1].strip().strip('"').strip("'")
            if not val:
                continue
            for fmt in ("%Y-%m-%d", "%Y/%m/%d", "%Y.%m.%d"):
                try:
                    d = _dt.strptime(val, fmt).date()
                    if d <= _date.today():
                        return True
                    break
                except Exception:
                    pass
    return False
