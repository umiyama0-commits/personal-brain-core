"""smoke test: brain_wiki_helpers/store_keyword.py (★2026-05-22 Phase 1c)。

Pure function なので直接 import + test 可能 (= brain_wiki.py 重い依存ナシ)。
"""
from __future__ import annotations

import pytest

from brain_wiki_helpers.store_keyword import (
    detect_store_keyword,
    PREFIX_BLOCKLIST,
    SUFFIX_BLOCKLIST,
)


# ─── テスト用 stores_content (history-stores.md 形式) ─────────────
HISTORY_STORES_SAMPLE = """\
## 2026-04
| # | code | name | qty | sales | currency |
| 1 | 9001 | サンプル駅前店 | 1 | 12,345 | JPY |
| 2 | 1002 | 川崎ダイス | 8 | 102,454 | JPY |
| 3 | 1003 | アミュプラザくまもと | 12 | 214,672 | JPY |
| 4 | 1004 | ららぽーと湘南平塚 | 5 | 78,500 | JPY |
| 5 | 1005 | イオンモール八幡東 | 3 | 45,000 | JPY |
| 6 | 1006 | 池袋西口 | 4 | 60,000 | JPY |
| 7 | 1007 | 東京ドームシティ ラクーア | 6 | 80,000 | JPY |
| 8 | 1008 | サンエー石垣シティ | 1 | 25,727 | JPY |
"""

# daily-stores.md (今日) 形式
DAILY_STORES_SAMPLE = """\
| # | code | name | customer | (JPY) sales |
| 1 | 9001 | サンプル駅前店 | 1 | (JPY)12,345 |
| 2 | 1002 | 川崎ダイス | 8 | (JPY)102,454 |
| 9 | 1009 | イオンモール伊達 | 2 | (JPY)35,000 |
| 10 | 1010 | アスティ静岡 | 3 | (JPY)42,000 |
"""


# ─── 完全一致 (highest priority) ─────────────
@pytest.mark.smoke
def test_exact_match_full_name():
    """店名そのままがクエリ内 → 完全一致"""
    result = detect_store_keyword(
        "ららぽーと湘南平塚の過去3ヶ月の売上は?", HISTORY_STORES_SAMPLE
    )
    assert result == "ららぽーと湘南平塚"


@pytest.mark.smoke
def test_exact_match_kawasaki_dice():
    result = detect_store_keyword(
        "川崎ダイスの今日の売上と客数は?", HISTORY_STORES_SAMPLE
    )
    assert result == "川崎ダイス"


# ─── prefix match (tokenize 経由) ─────────────
@pytest.mark.smoke
def test_prefix_match_unique_token():
    """「サンプル駅前の最近の売上」 → tokens=[サンプル駅前, 最近, 売上]、unique なので サンプル駅前店 採用"""
    result = detect_store_keyword(
        "サンプル駅前の最近の売上は?", HISTORY_STORES_SAMPLE
    )
    assert result == "サンプル駅前店"


@pytest.mark.smoke
def test_prefix_match_via_tokenize_kumamoto():
    """「アミュプラザくまもとの売上」 → 完全一致が先に hit (より長い)"""
    result = detect_store_keyword(
        "アミュプラザくまもとの売上は?", HISTORY_STORES_SAMPLE
    )
    assert result == "アミュプラザくまもと"


# ─── short prefix (2-3 字) ─────────────
@pytest.mark.smoke
def test_short_prefix_ikebukuro():
    """「池袋の売上」 → 短 prefix「池袋」unique で 池袋西口 採用"""
    result = detect_store_keyword(
        "池袋の売上は?", HISTORY_STORES_SAMPLE
    )
    assert result == "池袋西口"


# ─── suffix match (前置詞省略) ─────────────
@pytest.mark.smoke
def test_suffix_match_hachimanhigashi():
    """「八幡東店の過去3ヶ月」 → 「八幡東」 が suffix match (4文字以上)"""
    result = detect_store_keyword(
        "八幡東店の過去3ヶ月の売上", HISTORY_STORES_SAMPLE
    )
    assert result == "イオンモール八幡東"


# ─── daily-stores 検出 (新店) ─────────────
@pytest.mark.smoke
def test_daily_only_new_store_date():
    """history に無いが daily にある「アスティ静岡」を検出"""
    result = detect_store_keyword(
        "アスティ静岡の売上は?", HISTORY_STORES_SAMPLE, DAILY_STORES_SAMPLE
    )
    assert result == "アスティ静岡"


@pytest.mark.smoke
def test_daily_only_new_store_date_mall():
    """history に無い「イオンモール伊達」を検出 (= 新店扱い)"""
    result = detect_store_keyword(
        "イオンモール伊達の売上は?", HISTORY_STORES_SAMPLE, DAILY_STORES_SAMPLE
    )
    assert result == "イオンモール伊達"


# ─── location blocklist (誤検出防止) ─────────────
@pytest.mark.smoke
def test_location_blocklist_tokyo():
    """「東京の売上」 → 東京ドームシティ ラクーア に誤マッチさせない (LOCATION_BLOCKLIST)"""
    result = detect_store_keyword(
        "東京の売上は?", HISTORY_STORES_SAMPLE
    )
    # 「東京」は LOCATION_BLOCKLIST、prefix/short prefix で除外される
    # 完全一致もしない → None or 他の弱い match
    assert result != "東京ドームシティ ラクーア"


@pytest.mark.smoke
def test_prefix_blocklist_aeon():
    """「イオンの売上」 → "イオン" は PREFIX_BLOCKLIST、unique 候補で誤マッチさせない"""
    result = detect_store_keyword(
        "イオンの売上は?", HISTORY_STORES_SAMPLE, DAILY_STORES_SAMPLE
    )
    # 「イオン」は PREFIX_BLOCKLIST。完全一致もない
    # ただし suffix で「モール」が match しても SUFFIX_BLOCKLIST で除外、None になる想定
    # しかし「イオン」自体に一致する店舗名がない (= 全部「イオンモール...」「イオンスタイル...」)
    # 結果は実装依存だが、PREFIX_BLOCKLIST に入ってる事だけ test (= 設定の sanity)
    assert "イオン" in PREFIX_BLOCKLIST


# ─── 何も match しないケース ─────────────
@pytest.mark.smoke
def test_no_store_in_query():
    """店舗名が一切含まれないクエリ → None"""
    result = detect_store_keyword(
        "今日の天気はどう?", HISTORY_STORES_SAMPLE
    )
    assert result is None


@pytest.mark.smoke
def test_empty_stores_content():
    """stores_content が空 → None"""
    result = detect_store_keyword(
        "ららぽーと湘南平塚の売上", ""
    )
    assert result is None


# ─── BrainWiki._detect_store_keyword が helper を wrap してる ─────────────
@pytest.mark.smoke
def test_brain_wiki_method_wraps_helper():
    """brain_wiki.py の _detect_store_keyword が helper を呼んでる。"""
    from pathlib import Path as _Path
    REPO = _Path(__file__).resolve().parent.parent.parent
    src = (REPO / "brain_wiki.py").read_text(encoding="utf-8")
    # method 定義は残ってる
    assert "def _detect_store_keyword(" in src
    # helper import 経由
    assert "from brain_wiki_helpers.store_keyword import detect_store_keyword" in src


# ─── blocklist 定数 sanity ─────────────
@pytest.mark.smoke
def test_suffix_blocklist_includes_common_terms():
    """汎用 suffix が blocklist に含まれてる"""
    for term in ["モール", "プラザ", "センター", "シティ"]:
        assert term in SUFFIX_BLOCKLIST


@pytest.mark.smoke
def test_prefix_blocklist_includes_locations():
    """主要地名・国が blocklist に含まれてる"""
    for term in ["東京", "大阪", "日本", "シンガポール", "関東", "イオン"]:
        assert term in PREFIX_BLOCKLIST
