"""tests/smoke/test_feedback_rating_reason.py — 👍👎 の理由付与 + 集計 (採用レビュー #3).

★2026-07-11: 👎 の自由記述 (空約束) を 1タップ理由ボタンに置換。負の signal は tap 時点で
即保存 (理由未選択でも bad は残る)、理由 tap で同 record を enrich、週次で集計。
"""
from __future__ import annotations

from pathlib import Path

import pytest

import clone_feedback as cf


@pytest.fixture(autouse=True)
def _tmp_store(tmp_path, monkeypatch):
    monkeypatch.setattr(cf, "FEEDBACK_DIR", tmp_path)
    monkeypatch.setattr(cf, "RATING_AWAITING_FILE", tmp_path / ".rating_awaiting.json")
    yield


def test_bad_saved_immediately_with_null_reason():
    """👎 は tap 時点で bad + context 付きで保存 (理由未選択でも signal を失わない)。"""
    cf.start_rating("u1", "先週の売上は?", "先週は…", user_display="山田")
    rec = cf.save_rating("u1", "bad", user_display="山田")
    assert rec["rating"] == "bad"
    assert rec["reason"] is None
    assert rec["response"]  # context 保持


def test_attach_reason_enriches_latest_bad():
    cf.start_rating("u1", "q", "a" * 50)
    cf.save_rating("u1", "bad")
    assert cf.attach_reason("u1", "num") is True
    agg = cf.aggregate_ratings(days=7)
    assert agg["reason_counts"].get("num") == 1


def test_attach_reason_rejects_unknown_code():
    cf.start_rating("u1", "q", "a" * 50)
    cf.save_rating("u1", "bad")
    assert cf.attach_reason("u1", "bogus") is False


def test_attach_reason_no_target_returns_false():
    assert cf.attach_reason("nobody", "num") is False


def test_good_has_no_reason_and_not_counted_bad():
    cf.start_rating("u2", "q", "a" * 50)
    g = cf.save_rating("u2", "good")
    assert g["rating"] == "good" and g["reason"] is None
    agg = cf.aggregate_ratings(days=7)
    assert agg["good"] == 1 and agg["bad"] == 0


def test_aggregate_ratings_shape():
    for u in ("a", "b", "c"):
        cf.start_rating(u, f"q{u}", "resp" * 20)
        cf.save_rating(u, "bad")
    cf.attach_reason("a", "nodata")
    agg = cf.aggregate_ratings(days=7)
    assert agg["bad"] == 3 and agg["total"] == 3
    assert agg["reason_counts"].get("nodata") == 1
    assert 1 <= len(agg["recent_bad"]) <= 5
    assert "(理由未選択)" in [b["reason_label"] for b in agg["recent_bad"]]


def test_feedback_prompt_reason_buttons_are_ascii_postback():
    """理由ボタンの postback は ASCII (LW 制約) + label は 20 chars 内。"""
    from services import feedback_prompt as fp
    for b in fp._REASON_BUTTONS:
        assert b["data"].startswith("clonefb:why:")
        assert b["data"].isascii()
        assert len(b["label"]) <= 20


def test_cooldown_limits_prompt_frequency(monkeypatch):
    """★2026-07-12 海山「会話の全てに聞くのはやり過ぎ」: per-user cooldown で
    1 人あたり最大 週 1 回 (sampling rate と独立の上限)。"""
    monkeypatch.setenv("FEEDBACK_PROMPT_RATE", "1.0")
    from services import feedback_prompt as fp
    assert fp._should_prompt("u9", "x" * 60) is True
    fp._mark_prompted("u9")
    assert fp._should_prompt("u9", "y" * 60) is False   # cooldown 中
    assert fp._should_prompt("u10", "z" * 60) is True   # 別 user は無影響


def test_clonefb_routing_does_not_reference_unbound_user_display():
    """★2026-07-12 実バグ回帰 pin: clonefb routing が関数内未代入の user_display を
    参照して UnboundLocalError → 👍 tap がエラー返しになった (本番 08:55 実発)。
    parsed.get 経由であることを source-level で固定。"""
    src = (Path(__file__).resolve().parents[2] / "main.py").read_text(encoding="utf-8")
    block = src[src.index('if _pb_from_msg.startswith("clonefb:")'):][:600]
    assert "user_display=parsed.get(" in block
    assert "user_display=user_display" not in block
