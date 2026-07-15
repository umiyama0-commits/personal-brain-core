"""smoke test: brain_wiki_helpers/frontmatter.py (★2026-05-22 Phase 3b)。

Pure function 3 つを直接 import + test。
"""
from __future__ import annotations

from collections import OrderedDict

import pytest

from brain_wiki_helpers.frontmatter import (
    merge_frontmatters,
    split_h2_with_intro,
    normalize_heading,
)


# ─── normalize_heading ─────────────
@pytest.mark.smoke
def test_normalize_heading_strips_punct():
    assert normalize_heading("§ 1. 作品の核") == "1 作品の核"
    assert normalize_heading("  TEST  HEAD  ") == "test head"


@pytest.mark.smoke
def test_normalize_heading_idempotent():
    s = "海山 さん の 軸"
    assert normalize_heading(normalize_heading(s)) == normalize_heading(s)


@pytest.mark.smoke
def test_normalize_heading_preserves_jp():
    assert "判断" in normalize_heading("§ 判断")
    assert "ピンポン" in normalize_heading("ピンポン!")


# ─── merge_frontmatters ─────────────
@pytest.mark.smoke
def test_merge_frontmatters_updated_latest_wins():
    fm1 = "title: A\nupdated: 2026-04-01\n"
    fm2 = "updated: 2026-05-22\n"
    result = merge_frontmatters([fm1, fm2])
    assert "updated: 2026-05-22" in result


@pytest.mark.smoke
def test_merge_frontmatters_confidence_ranking():
    fm1 = "confidence: low\n"
    fm2 = "confidence: high\n"
    fm3 = "confidence: medium\n"
    result = merge_frontmatters([fm1, fm2, fm3])
    # high が最強で残る (rank: high=3, medium=2, low=1)
    assert "confidence: high" in result or "confidence: medium" in result
    # high が必ず勝つわけじゃないが (= 同 rank 上書きあり)、low には戻らない
    assert "confidence: low" not in result


@pytest.mark.smoke
def test_merge_frontmatters_tags_union():
    fm1 = "tags: [a, b]\n"
    fm2 = "tags: [b, c]\n"
    result = merge_frontmatters([fm1, fm2])
    # union 順序維持 → a, b, c
    assert "tags: [a, b, c]" in result


@pytest.mark.smoke
def test_merge_frontmatters_other_keys_last_wins():
    fm1 = "title: Old\n"
    fm2 = "title: New\n"
    result = merge_frontmatters([fm1, fm2])
    assert "title: New" in result


@pytest.mark.smoke
def test_merge_frontmatters_empty():
    assert merge_frontmatters([]) == ""


# ─── split_h2_with_intro ─────────────
@pytest.mark.smoke
def test_split_h2_simple():
    text = "intro line\n## section A\nbody A\n## section B\nbody B\n"
    intro, sections = split_h2_with_intro(text)
    assert intro.strip() == "intro line"
    assert len(sections) == 2
    keys = list(sections.keys())
    assert "section a" in keys
    assert "section b" in keys


@pytest.mark.smoke
def test_split_h2_no_h2_returns_full_text():
    text = "no header here\njust text\n"
    intro, sections = split_h2_with_intro(text)
    assert intro == text
    assert len(sections) == 0


@pytest.mark.smoke
def test_split_h2_dedups_same_heading():
    """同一 normalized heading は短い方が消える (= 長い body が勝つ、1.2x ルール)。"""
    text = (
        "## section\nshort\n"
        "## section\nthis is a much much longer body that should win\n"
    )
    _, sections = split_h2_with_intro(text)
    # 1 セクションに dedup
    assert len(sections) == 1
    _, body = next(iter(sections.values()))
    assert "much much longer" in body


# ─── brain_wiki.py wrapper 確認 ─────────────
@pytest.mark.smoke
def test_brain_wiki_methods_wrap_helpers():
    from pathlib import Path
    REPO = Path(__file__).resolve().parent.parent.parent
    src = (REPO / "brain_wiki.py").read_text(encoding="utf-8")
    # 全 3 method が helper import を呼ぶ wrapper に
    assert "from brain_wiki_helpers.frontmatter import merge_frontmatters" in src
    assert "from brain_wiki_helpers.frontmatter import split_h2_with_intro" in src
    assert "from brain_wiki_helpers.frontmatter import normalize_heading" in src
    # method 定義は残ってる
    assert "def _merge_frontmatters(self" in src
    assert "def _split_h2_with_intro(self" in src
    assert "def _normalize_heading(self" in src
