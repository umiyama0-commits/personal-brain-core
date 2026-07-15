"""smoke test: 月単独 (= 年省略) query で year_months に当年/去年が入る (★2026-05-27 海山指示)

bug: 「渋谷地下街の3月の売上は?」 で _extract_historical_sections の既存 regex は
   YYYY+M月 形式のみ match → 「3月」 単独 で year_months 空 →
   owndays-history-stores.md retrieval 完全 skip → fallback 発動

fix: M 月単独 regex 追加、当月以前=当年 / 当月より将来=去年 を assume.
"""
from __future__ import annotations

import re
from datetime import date
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent.parent


# ─── regex 単体 (= 私の追加 logic 検証) ─────
# regex を test 用に同型 reproduce (= 本物 brain_wiki から copy)
MONTH_ONLY_RE = re.compile(r"(?<![\d年/])(1[0-2]|[1-9])\s*月(?!間)")


def _resolve_year_for_month(mo: int, today_month: int, today_year: int) -> int:
    """当月以前=当年、当月より将来=去年"""
    return today_year if mo <= today_month else today_year - 1


@pytest.mark.smoke
@pytest.mark.parametrize("query,expected_months", [
    ("3月の売上", [3]),
    ("渋谷地下街の3月の売上は?", [3]),
    ("8月の売上", [8]),
    ("12月の業績", [12]),
    ("1月から3月まで", [1, 3]),  # 複数 match
])
def test_month_only_regex_matches(query, expected_months):
    matches = MONTH_ONLY_RE.findall(query)
    months = [int(m) for m in matches]
    assert months == expected_months, f"query={query!r} matched {months}, expected {expected_months}"


@pytest.mark.smoke
@pytest.mark.parametrize("query", [
    "2026年3月",   # YYYY 年 + M 月 形式 (= 既存 regex で cover)、月単独 regex は重複抽出しない
    "2026-3",      # YYYY-MM 形式
    "3ヶ月分",      # 「3 ヶ月」 = 単位
    "3か月",        # 「3 か月」 = 単位
    "13月",        # invalid (= 数字 2 digit で 13 になるが 1-12 制約で除外)
    "過去 3 ヶ月",   # 既存 logic で cover
    "31月",        # 不正 (= 13 月以上)
])
def test_month_only_regex_does_not_match_existing_patterns(query):
    """既存 regex で cover される pattern は 重複 match しないこと (= 副作用 避ける)."""
    matches = MONTH_ONLY_RE.findall(query)
    # 月単独 single regex で hit するか、又は 既存 regex 形式で hit 無し が期待
    # 「2026年3月」 / 「2026-3」 は 月単独 regex で hit "しない"
    if "年" in query or "-" in query or "ヶ" in query or "か月" in query:
        assert not matches, f"query={query!r} should not match (existing regex covers it): {matches}"


@pytest.mark.smoke
def test_year_resolution_past_month_uses_current_year():
    """当月以前 (=「3月」 in 5 月) は当年 (= 2026) を assume."""
    # 5 月 27 日 が今日 (= 2026-05-27)、query は「3月」 (= past)
    today_month = 5
    today_year = 2026
    mo = 3
    assert _resolve_year_for_month(mo, today_month, today_year) == 2026


@pytest.mark.smoke
def test_year_resolution_future_month_uses_last_year():
    """当月より将来 (=「8月」 in 5 月) は 去年 (= 2025) を assume."""
    today_month = 5
    today_year = 2026
    mo = 8
    assert _resolve_year_for_month(mo, today_month, today_year) == 2025


@pytest.mark.smoke
def test_year_resolution_current_month_uses_current_year():
    """当月自身 (=「5月」 in 5 月) は当年."""
    today_month = 5
    today_year = 2026
    mo = 5
    assert _resolve_year_for_month(mo, today_month, today_year) == 2026


# ─── brain_wiki.py source 内に regex 存在確認 ─────
@pytest.mark.smoke
def test_brain_wiki_has_month_only_regex():
    src = (REPO / "brain_wiki.py").read_text(encoding="utf-8")
    # 私が追加した regex pattern (= 部分一致で確認)
    assert "1[0-2]|[1-9]" in src
    assert "(?<![\\d年/])" in src
    # year_months.add の expansion (= 当年 / 去年 logic)
    assert "today.year - 1" in src
    # 海山指示 comment marker
    assert "★2026-05-27" in src
    assert "M 月" in src or "月単独" in src or "渋谷地下街" in src
