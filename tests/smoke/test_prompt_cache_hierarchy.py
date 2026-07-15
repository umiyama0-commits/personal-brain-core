"""smoke test: prompt cache 階層化 (★2026-05-23 海山指示 打ち手 B)

CLONE_PUBLIC_PROMPT を静的核 (= 規則・stance、~60K) と動的 context (= today + wiki、~20K) に
分離して Anthropic prompt caching を最大化する構造の sanity check。

実 LLM 呼出無し、純粋な文字列分割ロジック + 構造の検証。
"""
from __future__ import annotations

from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent.parent


# ─── _split_prompt_for_caching helper ─────────────
@pytest.mark.smoke
def test_split_returns_two_nonempty_parts_on_real_prompt():
    """実際の CLONE_PUBLIC_PROMPT を format してから split → 両方非空。"""
    import brain_wiki
    text = brain_wiki.CLONE_PUBLIC_PROMPT.format(
        today="2026-05-23",
        wiki_content="(dummy wiki content for smoke test)",
        few_shot_examples="(dummy few-shot)",
        dynamic_rules="",
        whitespace_url="https://example.test/whitespace?token=DUMMY",
    )
    static_prefix, dynamic_suffix = brain_wiki._split_prompt_for_caching(text)
    assert static_prefix, "static prefix が空 = 境界マーカーが見つからなかった"
    assert dynamic_suffix, "dynamic suffix が空"
    # 静的が動的より圧倒的に大きいこと (= cache 効率の前提)
    assert len(static_prefix) > len(dynamic_suffix) * 2, (
        f"静的部 ({len(static_prefix)}) が動的部 ({len(dynamic_suffix)}) より十分大きくない"
    )
    # 静的部に「ハルシネーション禁止」等の核規則が含まれる
    assert "ハルシネーション禁止" in static_prefix
    # 動的部に今日の日付と wiki content が含まれる
    assert "2026-05-23" in dynamic_suffix
    assert "dummy wiki content" in dynamic_suffix


@pytest.mark.smoke
def test_split_static_does_not_contain_placeholders():
    """静的部に format 後の今日の日付 / wiki_content が混ざってない (= cache 不可になる原因)。"""
    import brain_wiki
    text = brain_wiki.CLONE_PUBLIC_PROMPT.format(
        today="UNIQUE_DATE_2026",
        wiki_content="UNIQUE_WIKI_CONTENT",
        few_shot_examples="UNIQUE_FEW_SHOT",
        dynamic_rules="UNIQUE_DYNAMIC_RULES",
        whitespace_url="UNIQUE_WHITESPACE_URL",
    )
    static_prefix, _ = brain_wiki._split_prompt_for_caching(text)
    assert "UNIQUE_DATE_2026" not in static_prefix, "静的部に今日の日付が混入"
    assert "UNIQUE_WIKI_CONTENT" not in static_prefix, "静的部に wiki content が混入"
    assert "UNIQUE_DYNAMIC_RULES" not in static_prefix, "静的部に dynamic_rules (group 運用指示) が混入"


@pytest.mark.smoke
def test_split_returns_empty_when_boundary_missing():
    """境界マーカーが無い文字列は ("","") を返す (= caller が fallback できる)。"""
    from brain_wiki import _split_prompt_for_caching
    a, b = _split_prompt_for_caching("マーカー無しのテキスト")
    assert a == "" and b == ""


@pytest.mark.smoke
def test_boundary_marker_constant():
    """境界マーカー定数が prompt 内に存在する。"""
    import brain_wiki
    assert brain_wiki._CACHE_BOUNDARY_MARKER == "# 現在日時 (動的 context"
    # CLONE_PUBLIC_PROMPT 内に出現する
    assert brain_wiki._CACHE_BOUNDARY_MARKER in brain_wiki.CLONE_PUBLIC_PROMPT


# ─── CLONE_PUBLIC_PROMPT 構造 ─────────────
@pytest.mark.smoke
def test_clone_public_prompt_has_placeholders():
    """CLONE_PUBLIC_PROMPT に {today} と {wiki_content} が残っている。"""
    import brain_wiki
    assert "{today}" in brain_wiki.CLONE_PUBLIC_PROMPT
    assert "{wiki_content}" in brain_wiki.CLONE_PUBLIC_PROMPT


@pytest.mark.smoke
def test_clone_public_prompt_dynamic_section_at_end():
    """動的 section (= 今日の日付 + 参照可能な情報) が末尾近くに移動している。"""
    import brain_wiki
    prompt = brain_wiki.CLONE_PUBLIC_PROMPT
    today_idx = prompt.find("{today}")
    wiki_idx = prompt.find("{wiki_content}")
    # 両方 prompt の後半 70% 以降に位置する
    threshold = int(len(prompt) * 0.7)
    assert today_idx > threshold, f"today placeholder が prompt 前半 ({today_idx}/{len(prompt)})"
    assert wiki_idx > threshold, f"wiki_content placeholder が prompt 前半 ({wiki_idx}/{len(prompt)})"
    # 境界マーカーが両 placeholder 直前にある
    boundary_idx = prompt.find(brain_wiki._CACHE_BOUNDARY_MARKER)
    assert 0 < boundary_idx < today_idx, "境界マーカーが today より前にある必要"
    assert boundary_idx < wiki_idx


@pytest.mark.smoke
def test_clone_public_prompt_static_part_contains_core_rules():
    """静的部 (= 境界マーカー以前) に主要規則が含まれる。"""
    import brain_wiki
    text = brain_wiki.CLONE_PUBLIC_PROMPT.format(today="x", wiki_content="y", few_shot_examples="z", dynamic_rules="", whitespace_url="u")
    static_prefix, _ = brain_wiki._split_prompt_for_caching(text)
    # 5/23 までに追加された主要 axis が静的部に残ってる
    assert "ハルシネーション禁止" in static_prefix
    assert "AI 臭さ" in static_prefix or "AI臭さ" in static_prefix
    assert "ミラーリング" in static_prefix
    # OWNDAYS / 海山関連
    assert "OWNDAYS" in static_prefix
    assert "海山" in static_prefix


# ─── clone_respond_public の block 構造 (smart route) ─────────────
# ★2026-06-11 #9: 主経路は 3 block (v3 = persona / 今日+core / query依存)、
# v2 (2 block) は degrade 用に温存。終端アンカーも sentinel 除去版に追従。
_SMART_BLOCK_END_ANCHOR = (
    'else:\n            system_msg = '
    '{"role": "system", "content": _strip_core_sentinel(system_text)}'
)


@pytest.mark.smoke
def test_clone_respond_public_uses_three_block_for_smart():
    """smart route: v3 (3 block) が主経路、v2 (2 block) が fallback として残る。"""
    src = (REPO / "brain_wiki.py").read_text(encoding="utf-8")
    # v3 / v2 両方が smart route に存在
    assert "_split_prompt_for_caching_v3(system_text)" in src
    assert "_split_prompt_for_caching(system_text)" in src
    smart_block_start = src.find('if model.startswith("smart"):')
    assert smart_block_start > 0
    smart_block_end = src.find(_SMART_BLOCK_END_ANCHOR, smart_block_start)
    assert smart_block_end > smart_block_start
    smart_block = src[smart_block_start:smart_block_end]
    # v2 fallback (= 2 block 構造): static_prefix + dynamic_suffix
    assert "static_prefix" in smart_block
    assert "dynamic_suffix" in smart_block
    assert smart_block.count("cache_control") >= 1
    # fallback ガードあり (= boundary marker 見つからない時の安全側)
    assert "if static_prefix and dynamic_suffix" in smart_block
    # v3 主経路: 3 block (cache 2 + fresh 1) で _s1/_s2/_s3 を使う
    assert "_s1" in smart_block and "_s2" in smart_block and "_s3" in smart_block


@pytest.mark.smoke
def test_static_prefix_cache_uses_1h_ttl():
    """★2026-06-01 cost fix: 静的核 block の cache_control が 1h TTL。

    5分 TTL だと疎な DM traffic で静的核が毎回 cache 期限切れ→書き直し
    (cache-write が請求の ~70%、hit率 ~31%、Anthropic CSV 実測)。1h に延ばして
    営業時間中の再利用を read 化する。static_prefix block 限定 (動的 suffix には付けない)。
    """
    src = (REPO / "brain_wiki.py").read_text(encoding="utf-8")
    smart_block_start = src.find('if model.startswith("smart"):')
    assert smart_block_start > 0
    smart_block_end = src.find(_SMART_BLOCK_END_ANCHOR, smart_block_start)
    assert smart_block_end > smart_block_start
    smart_block = src[smart_block_start:smart_block_end]
    # static_prefix block 直後の cache_control に 1h TTL が付く
    sp_idx = smart_block.find('"text": static_prefix,')
    assert sp_idx > 0, "static_prefix block が見つからない"
    # comment 長に依存しないよう cache_control キー位置から検査
    cc_idx = smart_block.find('"cache_control":', sp_idx)
    assert cc_idx > sp_idx, "static_prefix 直後に cache_control キーが無い"
    cc_line = smart_block[cc_idx:cc_idx + 120]
    assert '"ttl": "1h"' in cc_line, (
        "静的核 cache_control に 1h TTL が無い (cost fix 未適用?)"
    )
    # 動的 suffix block (dynamic_suffix) には cache_control を付けない (= 毎回 fresh のまま)
    ds_idx = smart_block.find('"text": dynamic_suffix,')
    assert ds_idx > 0
    after_ds = smart_block[ds_idx:ds_idx + 300]
    # 実キー "cache_control": のみ検査 (コメント "# cache_control なし" は除外)
    assert '"cache_control":' not in after_ds, "動的 suffix に cache_control キーが付いている"
