"""smoke test: Plan C v2 Step 5 — retrieval 0 件 fallback (★2026-05-23 海山指示 + Reviewer fix)

`brain_wiki.BrainWiki._check_retrieval_fallback()` の判定 logic test。
実 chromadb / LLM call は走らず、stats dict を直接渡す pure function test。
"""
from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import MagicMock

import pytest

REPO = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(REPO))


@pytest.fixture
def fake_brain_wiki():
    """BrainWiki instance を最小限 mock (= _check_retrieval_fallback だけ呼べる)。"""
    import brain_wiki
    # __init__ を skip するため type の bare instance
    bw = brain_wiki.BrainWiki.__new__(brain_wiki.BrainWiki)
    return bw


@pytest.mark.smoke
def test_short_query_skip(fake_brain_wiki):
    """短文 query (< 3 文字) は fallback 対象外 → None。"""
    stats = {"vector_hits_total": 0, "vector_hits_high_conf": 0,
             "min_distance": None, "hist_included": False, "index_available": True}
    assert fake_brain_wiki._check_retrieval_fallback("?", stats) is None
    assert fake_brain_wiki._check_retrieval_fallback("うん", stats) is None
    assert fake_brain_wiki._check_retrieval_fallback("お疲れ", stats) is None
    assert fake_brain_wiki._check_retrieval_fallback("", stats) is None


@pytest.mark.smoke
def test_hist_included_bypass(fake_brain_wiki):
    """履歴 section 注入済 → context 持ってる、fallback 対象外。"""
    stats = {"vector_hits_total": 0, "vector_hits_high_conf": 0,
             "min_distance": None, "hist_included": True, "index_available": True}
    # 数字 keyword 含む長 query でも hist_included なら skip
    assert fake_brain_wiki._check_retrieval_fallback(
        "昨日の売上は?", stats
    ) is None


@pytest.mark.smoke
def test_index_unavailable_skip(fake_brain_wiki):
    """chromadb 接続不可 → 安全側で通常 flow に流す (= 海山判断、後で監視)。"""
    stats = {"vector_hits_total": 0, "vector_hits_high_conf": 0,
             "min_distance": None, "hist_included": False, "index_available": False}
    assert fake_brain_wiki._check_retrieval_fallback(
        "客単価教えて", stats
    ) is None


@pytest.mark.smoke
def test_high_conf_hit_skip(fake_brain_wiki):
    """高信頼 hit が 1 件以上 → context 持ってる、通常 flow。"""
    stats = {"vector_hits_total": 5, "vector_hits_high_conf": 1,
             "min_distance": 0.3, "hist_included": False, "index_available": True}
    assert fake_brain_wiki._check_retrieval_fallback(
        "客単価教えて", stats
    ) is None


@pytest.mark.smoke
def test_keyword_with_low_conf_only_triggers(fake_brain_wiki):
    """keyword 含 + 高信頼 hit 0 → fallback 発動。"""
    stats = {"vector_hits_total": 5, "vector_hits_high_conf": 0,
             "min_distance": 0.7, "hist_included": False, "index_available": True}
    result = fake_brain_wiki._check_retrieval_fallback(
        "客単価教えて", stats
    )
    assert result is not None
    assert "残念ながら" in result
    assert "確認できてない" in result


@pytest.mark.smoke
def test_zero_hits_no_keyword_skip(fake_brain_wiki):
    """retrieval 0 件でも keyword 無ければ通常 flow (= 雑談 / 一般質問は base LLM honesty 任せ)。"""
    stats = {"vector_hits_total": 0, "vector_hits_high_conf": 0,
             "min_distance": None, "hist_included": False, "index_available": True}
    result = fake_brain_wiki._check_retrieval_fallback(
        "経営について雑談しよう", stats  # keyword 含まず
    )
    # 雑談は fallback 対象外、base LLM が「データに無い」と返すなら通常 flow OK
    assert result is None


@pytest.mark.smoke
def test_no_keyword_with_hits_low_conf_no_fallback(fake_brain_wiki):
    """keyword 無 + hits ある (= 低信頼でも) → fallback しない (= base LLM の honesty 任せ)。"""
    stats = {"vector_hits_total": 3, "vector_hits_high_conf": 0,
             "min_distance": 0.8, "hist_included": False, "index_available": True}
    # 雑談 query (= keyword 含まず)
    result = fake_brain_wiki._check_retrieval_fallback(
        "経営について雑談しよう", stats
    )
    # keyword 無し + hits ある = 通常 flow、LLM が「データに無い」と honesty 発動する想定
    assert result is None


@pytest.mark.smoke
def test_fallback_text_matches_design(fake_brain_wiki):
    """fallback 文言が海山判断通り。"""
    import brain_wiki
    bw = brain_wiki.BrainWiki
    assert bw._RETRIEVAL_FALLBACK_TEXT == "残念ながらそれはこっちのデータに入ってないな。確認できてない。"
    assert bw._RETRIEVAL_FALLBACK_MIN_QUERY_LEN == 3
    # 主要 keyword 含まれる
    kws = bw._RETRIEVAL_FALLBACK_KEYWORDS
    assert "売上" in kws
    assert "客単価" in kws
    assert "FF" in kws
    assert "CVR" in kws
    assert "天神" in kws


@pytest.mark.smoke
def test_distance_threshold_boundary(fake_brain_wiki):
    """distance threshold (= 0.5) の境界判定。"""
    # 0.49 (= 高信頼) → fallback しない
    stats_high = {"vector_hits_total": 1, "vector_hits_high_conf": 1,
                  "min_distance": 0.49, "hist_included": False, "index_available": True}
    assert fake_brain_wiki._check_retrieval_fallback("客単価教えて", stats_high) is None

    # 0.51 (= 低信頼、high_conf カウント 0) → fallback 発動
    stats_low = {"vector_hits_total": 1, "vector_hits_high_conf": 0,
                 "min_distance": 0.51, "hist_included": False, "index_available": True}
    result = fake_brain_wiki._check_retrieval_fallback("客単価教えて", stats_low)
    assert result is not None
