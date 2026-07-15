"""#9 core cache split の byte-identity 検証 (★2026-06-11)。

設計の根拠: モデルが見る system prompt の連結テキストは従来 (sentinel 導入前) と
完全 byte 一致で、変わるのは cache_control の置き場所 (課金境界) だけ。
このテストが green である限り「キャッシュ分割で応答品質は変わらない」が構成的に成立。
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))

import brain_wiki  # noqa: E402
from brain_wiki import (  # noqa: E402
    _CACHE_BOUNDARY_MARKER,
    _CORE_CACHE_SENTINEL,
    _split_prompt_for_caching,
    _split_prompt_for_caching_v3,
    _strip_core_sentinel,
)

PERSONA = "あなたは海山丈司。\n\n# ルール\n値引きしない。\n\n"
TODAY_SEC = _CACHE_BOUNDARY_MARKER + "、説明)\n今日の日付: 2026-06-11\n\n# 参照可能な情報\n"
CORE = "=== [CANONICAL] identity.md ===\n本質直球。\n\n=== [CANONICAL] knowledge/owndays-daily-sales.md ===\n全社 1.2M"
REST = "=== knowledge/owndays-history-stores.md (渋谷) ===\n| 1 | 123 | 渋谷 | 500 | 7,000,000 |"


def _build(with_sentinel: bool, with_rest: bool = True) -> str:
    wiki = CORE
    if with_sentinel:
        wiki += "\n\n" + _CORE_CACHE_SENTINEL
    if with_rest:
        wiki += "\n\n" + REST
    return PERSONA + TODAY_SEC + wiki + "\n"


def test_v3_splits_into_three_parts():
    full = _build(with_sentinel=True)
    s1, s2, s3 = _split_prompt_for_caching_v3(full)
    assert s1 and s2 and s3
    assert s1 == PERSONA.rstrip()          # v2 と同じ rstrip 仕様
    assert s2.startswith(_CACHE_BOUNDARY_MARKER)
    assert "[CANONICAL]" in s2             # core は block2
    assert "history-stores" in s3          # query 依存部は block3
    assert _CORE_CACHE_SENTINEL not in (s1 + s2 + s3)  # 番兵はモデル不可視


def test_v3_byte_identity_with_legacy_output():
    """★最重要: 3 片の連結 == 従来出力 (sentinel 無し時代) と byte 一致。"""
    full = _build(with_sentinel=True)
    legacy = _build(with_sentinel=False)   # 旧挙動 (gate OFF) の出力
    s1, s2, s3 = _split_prompt_for_caching_v3(full)
    v2_static, v2_dynamic = _split_prompt_for_caching(legacy)
    assert s1 + s2 + s3 == v2_static + v2_dynamic
    # strip でも同じ形に戻る (fallback 経路の保証)
    assert _strip_core_sentinel(full) == legacy


def test_v3_whitespace_rest_folds_into_s2():
    """★Devil's Advocate blocker fix (2026-06-11): 履歴/vector/メモリーが空の最頻 DM 形。

    旧実装は s3="\\n" (whitespace のみ) を返し、Anthropic が 400 で拒否 → litellm が
    silent に gpt-4o へ fallback する事故 (実 API 検証済)。修正後は whitespace を s2 に
    畳み s3="" → caller は 2 block (両方 cache)。byte-identity は維持。
    """
    full = _build(with_sentinel=True, with_rest=False)
    s1, s2, s3 = _split_prompt_for_caching_v3(full)
    assert s1 and s2
    assert s3 == ""                      # whitespace-only block を作らせない
    assert s2.endswith("\n")             # 末尾改行は s2 に畳まれる
    legacy = _build(with_sentinel=False, with_rest=False)
    v2_static, v2_dynamic = _split_prompt_for_caching(legacy)
    assert s1 + s2 + s3 == v2_static + v2_dynamic  # byte-identity 維持


def test_v3_s3_never_whitespace_only():
    """契約: s3 は空文字 or 非空白含有のどちらか (whitespace-only は禁止 = 400 の根)。"""
    for with_rest in (True, False):
        _, _, s3 = _split_prompt_for_caching_v3(_build(True, with_rest=with_rest))
        assert s3 == "" or s3.strip()


def test_v3_degrades_without_sentinel():
    assert _split_prompt_for_caching_v3(_build(with_sentinel=False)) == ("", "", "")


def test_v3_degrades_without_persona_marker():
    text = "マーカー無し" + "\n\n" + _CORE_CACHE_SENTINEL + "\n\nrest"
    assert _split_prompt_for_caching_v3(text) == ("", "", "")


def test_strip_removes_bare_sentinel_too():
    assert _strip_core_sentinel("a" + _CORE_CACHE_SENTINEL + "b") == "ab"
    assert _CORE_CACHE_SENTINEL not in _strip_core_sentinel(_build(True))


def test_assembly_inserts_sentinel_gated():
    """組み立て側 (compact) に env gate 付き sentinel 挿入があるか (構造検証)。"""
    src = Path(brain_wiki.__file__).read_text(encoding="utf-8")
    fn_start = src.find("def _read_wiki_state_public_compact")
    fn_end = src.find("\n    def ", fn_start + 1)
    body = src[fn_start:fn_end]
    assert "CORE_CACHE_SPLIT_ENABLED" in body
    assert "_CORE_CACHE_SENTINEL" in body
    # 予算計算を変えない (acc に数えない) 設計コメントの目印
    assert "acc には数えない" in body


def test_respond_uses_v3_and_strips_everywhere():
    """応答側: v3 利用 + 全 fallback/非smart 経路で sentinel 除去 (構造検証)。"""
    src = Path(brain_wiki.__file__).read_text(encoding="utf-8")
    fn_start = src.find("async def clone_respond_public")
    fn_end = src.find("\n    async def ", fn_start + 1)
    body = src[fn_start:fn_end]
    assert "_split_prompt_for_caching_v3" in body
    assert body.count("_strip_core_sentinel") >= 2  # v2経路 + 非smart経路


def test_assembly_gate_on_off_behavioral_equality(monkeypatch, tmp_path):
    """★Reviewer 推奨 (cross-check 2026-06-11): gate ON の出力から sentinel を
    除去すると gate OFF の出力と byte 一致 + retrieval_stats 同一 (挙動レベルの不変性)。
    """
    import asyncio

    wiki = tmp_path / "wiki"
    (wiki / "knowledge").mkdir(parents=True)
    (wiki / "identity.md").write_text(
        "---\nclone_visibility: public\n---\n本質直球で考える。\n",
        encoding="utf-8",
    )
    (wiki / "knowledge" / "owndays-daily-sales.md").write_text(
        "---\nclone_visibility: public\n---\n全社売上 1.2M\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(brain_wiki, "WIKI_DIR", wiki)
    # /app 配下の mkdir を回避 (local テストでは存在しないため)
    monkeypatch.setattr(brain_wiki.BrainWiki, "_ensure_dirs", lambda self: None)
    bw = brain_wiki.BrainWiki(http=None, litellm_url="http://x", litellm_key="k")
    bw.index = None  # vector search 無効 (決定論)

    async def _run() -> tuple:
        monkeypatch.setenv("CORE_CACHE_SPLIT_ENABLED", "1")
        on_text, on_stats = await bw._read_wiki_state_public_compact("おはよう")
        monkeypatch.setenv("CORE_CACHE_SPLIT_ENABLED", "0")
        off_text, off_stats = await bw._read_wiki_state_public_compact("おはよう")
        return on_text, on_stats, off_text, off_stats

    on_text, on_stats, off_text, off_stats = asyncio.run(_run())
    assert _CORE_CACHE_SENTINEL in on_text
    assert _CORE_CACHE_SENTINEL not in off_text
    assert _strip_core_sentinel(on_text) == off_text  # ★挙動レベル byte 一致
    assert on_stats == off_stats                      # 予算/stats 不変
