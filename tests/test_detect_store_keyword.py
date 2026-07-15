"""
店舗キーワード検出 (_detect_store_keyword) の網羅テスト。

★2026-05-15 追加: 武蔵小山事故 (「武蔵小山の最近の売上は?」が None で bot が
「データない」と誤回答) を契機に、店名 prefix / suffix / 助詞付きクエリ /
地名誤検出 等の主要パターンを永続テスト化。

実行: docker exec line-bot python3 -m pytest /app/tests/test_detect_store_keyword.py -v
(host Python 3.9 では brain_wiki の `str | None` 型注釈がエラーになるため、
docker 内 Python 3.12 で実行する)
"""
import sys
from pathlib import Path

sys.path.insert(0, "/app")

import pytest


@pytest.fixture(scope="module")
def detector():
    from brain_wiki import BrainWiki
    return BrainWiki.__new__(BrainWiki)


@pytest.fixture(scope="module")
def store_contents():
    """実際の wiki content を fixture として読み込む"""
    WIKI = Path("/app/data/brain/wiki")
    stores_p = WIKI / "knowledge" / "owndays-history-stores.md"
    daily_p = WIKI / "knowledge" / "owndays-daily-stores.md"
    if not stores_p.exists() or not daily_p.exists():
        pytest.skip("wiki/knowledge/owndays-*-stores.md が無い")
    return (
        stores_p.read_text(encoding="utf-8"),
        daily_p.read_text(encoding="utf-8"),
    )


# ─── 検出すべきケース ───────────────────────
DETECT_CASES = [
    ("武蔵小山の最近の売上は?", "サンプル駅前店"),
    ("武蔵小山の売上", "サンプル駅前店"),
    ("武蔵小山店の売上", "サンプル駅前店"),
    ("武蔵小山店舗の客数", "サンプル駅前店"),
    ("池袋の売上は?", "池袋西口"),
    ("川崎の売上は?", "川崎ダイス"),
    ("ららぽーと湘南の売上は?", "ららぽーと湘南平塚"),
    ("ららぽーと福岡の客数は?", "ららぽーと福岡"),
    ("吉祥寺の売上", "吉祥寺"),
    ("錦糸町の売上", "錦糸町マルイ"),
    ("上野の売上", "上野マルイ"),
    ("北千住の売上", "北千住マルイ"),
    ("赤羽の売上", "赤羽ビビオ"),
    ("新宿東口の売上", "新宿東口"),
    ("新宿マルイアネックスの売上", "新宿マルイアネックス"),
    ("八幡東店の売上", "イオンモール八幡東"),
    ("川崎ダイスの売上", "川崎ダイス"),
    ("アミュプラザくまもとの売上", "アミュプラザくまもと"),
    ("吉祥寺の最近の推移を教えて", "吉祥寺"),
]


@pytest.mark.parametrize("query,expected", DETECT_CASES)
def test_detect_store(detector, store_contents, query, expected):
    stores, daily = store_contents
    result = detector._detect_store_keyword(query, stores, daily)
    assert result == expected, f"query={query!r} → 期待:{expected!r}, 実:{result!r}"


# ─── 検出すべきでないケース (誤検出防止) ──────
REJECT_CASES = [
    "東京の売上",
    "日本の売上",
    "シンガポールの売上",
    "渋谷の売上",
    "新宿の売上",
    "全店の売上",
    "世界の売上",
    "AOPの達成率は",
    "VMVについて",
]


@pytest.mark.parametrize("query", REJECT_CASES)
def test_reject_non_store(detector, store_contents, query):
    stores, daily = store_contents
    result = detector._detect_store_keyword(query, stores, daily)
    assert result is None, f"query={query!r} → 誤検出: {result!r} (None 期待)"
