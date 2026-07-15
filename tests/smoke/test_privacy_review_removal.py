"""smoke test: clone_memory_privacy_review._remove_private_lines (★2026-06-07 cross-check hardened)

DA cross-check が PII gate の致命的取りこぼし (bullet prefix 不一致 / 20字未満短PII / hashtag本文) を
指摘し、hardened した除去ロジックの回帰保護。destructive な PII 削除なので test 必須。
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(REPO / "scripts"))


def _fn():
    from clone_memory_privacy_review import _remove_private_lines
    return _remove_private_lines


@pytest.mark.smoke
def test_bullet_prefix_and_short_pii_purged():
    """DA#1: clone_memory は `- ...` list 形式。LLM が bullet 無しで返す短PII (8字) も完全 purge。重複も全除去。"""
    content = "# Profile\n- 田中は鬱で休職中\n好きな食べ物はラーメン\n- 田中は鬱で休職中"
    private = [{"line": "田中は鬱で休職中", "category": "health", "reason": "病名"}]
    new, removed = _fn()(content, private)
    assert "田中は鬱" not in new, "bullet 無しで返された短PII も完全 purge (旧 exact-only では残留していた)"
    assert len(removed) == 2, "重複行を全除去"
    assert "ラーメン" in new and "# Profile" in new, "無関係行・section 見出しは保護"


@pytest.mark.smoke
def test_hashtag_body_pii_removed_headings_protected():
    """DA#3: 本文 #hashtag PII は除去、section 見出し (# Profile / ## Key Facts) は保護。"""
    content = "# Profile\n#田中さんと面談、奥さんと別居中\n## Key Facts\n- 普通の話"
    private = [{"line": "田中さんと面談、奥さんと別居中", "category": "family", "reason": "別居"}]
    new, removed = _fn()(content, private)
    assert "別居中" not in new
    assert "# Profile" in new and "## Key Facts" in new, "section 見出しは全 # 除外せず保護"


@pytest.mark.smoke
def test_exact_match_consumes_so_long_line_protected():
    """誤爆抑制: 短PII が別の長行の substring でも、完全一致行が消費するので長行を巻込まない。"""
    content = "- 名前は田中\n- 名前は田中さんは良い人で長い文の一部として登場する内容ここ"
    private = [{"line": "名前は田中", "category": "pii", "reason": "実名"}]
    new, removed = _fn()(content, private)
    after = new.split("\n")
    assert "- 名前は田中" not in after, "完全一致行は除去"
    assert any("良い人" in l for l in after), "substring を含む長行は保護"
    assert len(removed) == 1


@pytest.mark.smoke
def test_partial_match_requires_20_chars():
    """部分一致 fallback は 20字以上限定。完全一致の無い短 text は除去しない (誤爆防止)。"""
    content = "- 田中さんと打合せした長い行で色々な話題が出た記録がここに残っている"
    private = [{"line": "田中", "category": "pii", "reason": "実名"}]
    new, removed = _fn()(content, private)
    assert len(removed) == 0 and "田中さんと打合せ" in new, "20字未満の部分一致は発火しない"


@pytest.mark.smoke
def test_non_dict_item_no_crash():
    """Reviewer note: private_lines に非 dict が混じっても AttributeError で落ちない。"""
    content = "- 田中は鬱で休職中"
    new, removed = _fn()(content, ["不正な文字列要素", {"line": "田中は鬱で休職中", "category": "h", "reason": "r"}])
    assert "田中は鬱" not in new and len(removed) == 1


@pytest.mark.smoke
def test_empty_private_lines_noop():
    content = "- 普通の内容\n好きな食べ物"
    new, removed = _fn()(content, [])
    assert new == content and removed == []
