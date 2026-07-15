"""smoke test: query intent 検出 + target_chars 動的配分 (項目 2)。

brain_wiki.py の _read_wiki_state_public_compact 内に hardcode された
CORE_WIKI_REGISTRY / CATEGORY_BOOST_BY_INTENT / _detect_query_intent /
_target_chars のロジックを **source 検査 + 純粋計算で** 検証。

実 retrieval は重いので integration test に回す。
"""
from __future__ import annotations

import re
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
BRAIN_WIKI_PY = REPO_ROOT / "brain_wiki.py"


@pytest.mark.smoke
def test_core_wiki_registry_present():
    """CORE_WIKI_REGISTRY が brain_wiki.py に存在する。"""
    src = BRAIN_WIKI_PY.read_text(encoding="utf-8")
    assert "CORE_WIKI_REGISTRY" in src
    # 主要 wiki が登録されてる
    must = [
        "identity.md", "style.md", "thinking.md",
        "knowledge/owndays-vmv.md",
        "style/style-no-claude-proposals.md",
        "style/response-bank.md",
        "style/style-depth-as-undercurrent.md",
        "hobbies/index.md",
    ]
    # registry の dict literal を切り出して中身検査
    m = re.search(
        r"CORE_WIKI_REGISTRY[^=]*=\s*\{(.*?)\n\s{8}\}", src, re.DOTALL
    )
    assert m, "CORE_WIKI_REGISTRY の dict literal が見つからない"
    body = m.group(1)
    for path in must:
        assert path in body, f"CORE_WIKI_REGISTRY に {path} が登録されてない"


@pytest.mark.smoke
def test_category_boost_intents_defined():
    """CATEGORY_BOOST_BY_INTENT に主要 intent (sales/hobbies/consultation/judgment/business/general) が定義されてる。"""
    src = BRAIN_WIKI_PY.read_text(encoding="utf-8")
    m = re.search(
        r"CATEGORY_BOOST_BY_INTENT[^=]*=\s*\{(.*?)\n\s{8}\}", src, re.DOTALL
    )
    assert m, "CATEGORY_BOOST_BY_INTENT が見つからない"
    body = m.group(1)
    for intent in ("sales", "hobbies", "consultation", "judgment", "business", "general"):
        assert f'"{intent}"' in body, f"intent {intent} が未定義"


@pytest.mark.smoke
def test_sales_intent_keywords_in_query():
    """売上 query 系のキーワードが SALES_KEYWORDS に含まれる。"""
    src = BRAIN_WIKI_PY.read_text(encoding="utf-8")
    m = re.search(r"SALES_KEYWORDS\s*=\s*\(([^)]+)\)", src, re.DOTALL)
    assert m
    body = m.group(1)
    for kw in ("売上", "客数", "客単価", "業績"):
        assert kw in body


@pytest.mark.smoke
def test_intent_detector_function_present():
    """_detect_query_intent 関数が定義されている。"""
    src = BRAIN_WIKI_PY.read_text(encoding="utf-8")
    assert "def _detect_query_intent" in src
    # 嗜好系キーワードの一部が body に含まれてる
    m = re.search(r"def _detect_query_intent.*?return \"general\"", src, re.DOTALL)
    assert m, "intent detector 関数が完結してない"
    body = m.group(0)
    # 主要 intent 判定がされる
    for kw in ("漫画", "マンガ", "音楽", "やる気", "判断", "OWNDAYS"):
        assert kw in body, f"intent detector に {kw} が無い"


@pytest.mark.smoke
def test_target_chars_clamp_logic_present():
    """target_chars が 800-12000 にクランプされる。"""
    src = BRAIN_WIKI_PY.read_text(encoding="utf-8")
    m = re.search(r"def _target_chars.*?return max\(800, min\(target, 12000\)\)", src, re.DOTALL)
    assert m, "target_chars のクランプロジックが無い (800-12000)"


@pytest.mark.smoke
def test_truncate_uses_target_chars():
    """truncate ロジックが固定 7000 ではなく _target_chars を使う。"""
    src = BRAIN_WIKI_PY.read_text(encoding="utf-8")
    # 旧 "if len(content) > 7000:" は core_files_truncated ループから消えてるはず
    # 代わりに target = _target_chars(name) が呼ばれてる
    # ★2026-07-03 P3b: CORE_BUDGET 有効時の縮小済み target 優先形も許容
    m = re.search(
        r"for name in core_files_truncated:.*?target = _scaled_targets\.get\(name, _target_chars\(name\)\)",
        src, re.DOTALL
    )
    assert m, "core_files_truncated ループで _target_chars (scaled 優先形) が使われてない"


@pytest.mark.smoke
def test_sales_boost_higher_than_hobbies_boost_for_sales_intent():
    """sales intent では sales > hobbies の boost であること (順序検査)。"""
    src = BRAIN_WIKI_PY.read_text(encoding="utf-8")
    m = re.search(
        r'"sales":\s*\{([^}]+)\}', src, re.DOTALL
    )
    assert m
    body = m.group(1)
    # sales: 2.0 / hobbies: 0.3 のオーダー
    sales_m = re.search(r'"sales":\s*([\d.]+)', body)
    hobbies_m = re.search(r'"hobbies":\s*([\d.]+)', body)
    assert sales_m and hobbies_m
    assert float(sales_m.group(1)) > float(hobbies_m.group(1))


@pytest.mark.smoke
def test_hobbies_boost_higher_for_hobbies_intent():
    """hobbies intent では hobbies が他より優遇される。"""
    src = BRAIN_WIKI_PY.read_text(encoding="utf-8")
    m = re.search(
        r'"hobbies":\s*\{([^}]+)\}', src, re.DOTALL
    )
    assert m
    body = m.group(1)
    hobbies_m = re.search(r'"hobbies":\s*([\d.]+)', body)
    sales_m = re.search(r'"sales":\s*([\d.]+)', body)
    assert hobbies_m and sales_m
    # hobbies intent では hobbies > sales
    assert float(hobbies_m.group(1)) > float(sales_m.group(1))


@pytest.mark.smoke
def test_consultation_intent_boosts_style_identity():
    """consultation intent では style / identity が高 boost。"""
    src = BRAIN_WIKI_PY.read_text(encoding="utf-8")
    m = re.search(
        r'"consultation":\s*\{([^}]+)\}', src, re.DOTALL
    )
    assert m
    body = m.group(1)
    style_m = re.search(r'"style":\s*([\d.]+)', body)
    identity_m = re.search(r'"identity":\s*([\d.]+)', body)
    assert style_m and identity_m
    assert float(style_m.group(1)) >= 1.3
    assert float(identity_m.group(1)) >= 1.3
