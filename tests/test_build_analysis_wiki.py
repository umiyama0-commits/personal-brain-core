"""共有 emitter build_analysis_wiki (cross-check S5/S2/S6) のガード検証.

★2026-06-10: サブPJ×brain 連携の frontmatter/visibility/freshness/PJ分類 を一元管理する
emitter。横展開時の private 漏れ・鮮度の嘘・数値PJ の断定を構造的に防ぐガードをテスト。
"""
import sys
from datetime import date
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import build_analysis_wiki as bw  # noqa: E402


@pytest.fixture
def tmp_analysis(tmp_path, monkeypatch):
    monkeypatch.setattr(bw, "ANALYSIS_DIR", tmp_path)
    return tmp_path


def _call(tmp, **over):
    kw = dict(
        pj_id="test-pj", title="T", overview="o",
        sections=[("H", "B")], pj_class="static-factual", sources=["s.csv"],
    )
    kw.update(over)
    return bw.build_analysis_wiki(**kw)


def test_static_factual_ok(tmp_analysis):
    out = _call(tmp_analysis)
    txt = out.read_text(encoding="utf-8")
    assert "clone_visibility: private" in txt          # S2: デフォルト private
    assert f"updated: {date.today().isoformat()}" in txt  # S3: 計算された日付
    assert "valid_until:" in txt                       # S3: 自動退場
    assert "pj_class: static-factual" in txt           # S6


def test_invalid_pj_class_rejected(tmp_analysis):
    with pytest.raises(ValueError, match="pj_class"):
        _call(tmp_analysis, pj_class="whatever")


def test_model_estimate_requires_assumptions(tmp_analysis):
    """S6: model-estimate は不確実性(assumptions)無しだと拒否。"""
    with pytest.raises(ValueError, match="assumptions"):
        _call(tmp_analysis, pj_class="model-estimate", assumptions=None)


def test_model_estimate_inlines_uncertainty(tmp_analysis):
    """S6: model-estimate は wiki 冒頭に「推定値」警告 + 前提を inline。"""
    out = _call(tmp_analysis, pj_class="model-estimate",
                assumptions=["前提A", "前提B"])
    txt = out.read_text(encoding="utf-8")
    assert "推定値" in txt
    assert "前提A" in txt and "前提B" in txt
    assert "pj_class: model-estimate" in txt


def test_model_estimate_shorter_validity(tmp_analysis):
    """S6: model-estimate は static より valid_until が短い(陳腐化が早い)。"""
    static = _call(tmp_analysis, pj_id="s").read_text(encoding="utf-8")
    model = _call(tmp_analysis, pj_id="m", pj_class="model-estimate",
                  assumptions=["a"]).read_text(encoding="utf-8")
    s_vu = [l for l in static.splitlines() if l.startswith("valid_until:")][0]
    m_vu = [l for l in model.splitlines() if l.startswith("valid_until:")][0]
    assert m_vu < s_vu  # model の期限の方が手前(文字列比較で ISO 日付は順序一致)


def test_public_requires_cosign(tmp_analysis):
    """S2: public は allow_public の明示 co-sign が必要(意図的漏れ防止)。"""
    with pytest.raises(ValueError, match="allow_public"):
        _call(tmp_analysis, visibility="public")
    # co-sign すれば通る
    out = _call(tmp_analysis, visibility="public", allow_public=True)
    assert "clone_visibility: public" in out.read_text(encoding="utf-8")


def test_model_estimate_cannot_be_public(tmp_analysis):
    """S6: 不確実な数値の社員公開は禁止。"""
    with pytest.raises(ValueError, match="public"):
        _call(tmp_analysis, pj_class="model-estimate", assumptions=["a"],
              visibility="public", allow_public=True)
