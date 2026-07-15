"""tests/test_data_gaps.py — 「データ無い」 検出 + queue + dashboard tests

★2026-05-26 海山指示: 「データがない」回答を「今後拡充予定」 tone に転換 +
そういう回答を拾って「データ拡充候補」 cycle を作る。
"""
from __future__ import annotations

import importlib
import json
import sys
import time
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))
sys.path.insert(0, str(REPO_ROOT / "scripts"))


@pytest.fixture
def tmp_brain(tmp_path, monkeypatch):
    monkeypatch.setenv("BRAIN_ROOT", str(tmp_path))
    if "services.data_gaps" in sys.modules:
        del sys.modules["services.data_gaps"]
    mod = importlib.import_module("services.data_gaps")
    mod.GAPS_DIR = tmp_path / "clone_review"
    mod.GAPS_FILE = mod.GAPS_DIR / "data_gaps.jsonl"
    return mod


# ─── detector tests ─────────────────────────────────
def test_detect_basic_patterns():
    from data_gap_detector import detect_data_gap
    cases = [
        ("x", "申し訳ありません、データがないです", "no_data"),
        ("x", "情報がありません", "no_info"),
        ("x", "記録が無い", "no_record"),
        ("x", "把握できていない", "not_grasped"),
        ("x", "持っていないんだよね", "not_held"),
        ("x", "そっち流し込めてないんだよ", "not_ingested"),
        ("x", "それ分からない", "dunno"),
        ("x", "答えられません", "cant_answer"),
        ("x", "集計中だね", "not_aggregated"),
    ]
    for query, response, expected_cat in cases:
        info = detect_data_gap(query, response)
        assert info is not None, f"failed to detect: {response}"
        assert info["category"] == expected_cat, f"wrong cat for {response}: got {info['category']}"


def test_detect_no_false_positive():
    """通常の数字応答は検出しない"""
    from data_gap_detector import detect_data_gap
    assert detect_data_gap("売上は?", "全社売上 100M 円、予算比 95%") is None
    assert detect_data_gap("吉祥寺は?", "10 人、114,590 円、客単価 11,459 円") is None


def test_detect_forward_looking_flag():
    """既に「今後拡充」 tone を含む応答も検出はする (= flag true)"""
    from data_gap_detector import detect_data_gap
    info = detect_data_gap("x", "そこ、データ無いね。今後集めて少しずつ更新する予定")
    assert info is not None
    assert info["forward_looking"] is True

    info2 = detect_data_gap("x", "データ無いです")
    assert info2 is not None
    assert info2["forward_looking"] is False


def test_detect_empty_response():
    from data_gap_detector import detect_data_gap
    assert detect_data_gap("x", "") is None
    assert detect_data_gap("x", None) is None


# ─── service tests ─────────────────────────────────
def test_auto_capture_basic(tmp_brain):
    mod = tmp_brain
    fid = mod.auto_capture(
        user_query="武蔵小山の去年の客単価は?",
        bot_response="データがありません",
        user_id="u1",
        matched_category="no_data",
        matched_text="データがありません",
    )
    assert fid.startswith("gap_")
    active = mod.list_active()
    assert len(active) == 1
    assert active[0]["occurrence_count"] == 1


def test_auto_capture_dedupe_same_query(tmp_brain):
    """同 normalized query + category → occurrence_count +1"""
    mod = tmp_brain
    f1 = mod.auto_capture(
        user_query="武蔵小山の去年の客単価は?",
        bot_response="data 無し",
        matched_category="no_data",
    )
    time.sleep(0.01)
    f2 = mod.auto_capture(
        user_query="武蔵小山の去年の客単価は?",  # same query
        bot_response="再 data 無し",
        matched_category="no_data",
    )
    assert f1 == f2  # 同 record
    items = mod.list_active()
    assert len(items) == 1
    assert items[0]["occurrence_count"] == 2


def test_auto_capture_different_category_separate(tmp_brain):
    mod = tmp_brain
    f1 = mod.auto_capture(
        user_query="武蔵小山の去年の客単価は?",
        bot_response="data 無し",
        matched_category="no_data",
    )
    f2 = mod.auto_capture(
        user_query="武蔵小山の去年の客単価は?",  # 同 query
        bot_response="分からない",
        matched_category="dunno",  # 違う category
    )
    assert f1 != f2
    items = mod.list_active()
    assert len(items) == 2


def test_normalize_query_masks_numbers(tmp_brain):
    """数字 mask により 直近 3 日 と 直近 7 日 が同一視される (= mask=<N>)"""
    mod = tmp_brain
    f1 = mod.auto_capture(
        user_query="直近 3 日の売上",
        bot_response="data 無し",
        matched_category="no_data",
    )
    f2 = mod.auto_capture(
        user_query="直近 7 日の売上",  # 数字違うだけ
        bot_response="data 無し",
        matched_category="no_data",
    )
    assert f1 == f2  # mask 一致 → 同 record
    items = mod.list_active()
    assert len(items) == 1
    assert items[0]["occurrence_count"] == 2


def test_update_status(tmp_brain):
    mod = tmp_brain
    fid = mod.auto_capture(
        user_query="x",
        bot_response="data 無し",
        matched_category="no_data",
    )
    assert mod.update_status(fid, "planned")
    assert mod.count_active() == 1  # planned もアクティブ
    assert mod.update_status(fid, "done")
    assert mod.count_active() == 0  # done は除外


def test_update_status_invalid(tmp_brain):
    mod = tmp_brain
    fid = mod.auto_capture(user_query="x", bot_response="data 無し", matched_category="no_data")
    assert not mod.update_status(fid, "garbage")


def test_add_comment(tmp_brain):
    mod = tmp_brain
    fid = mod.auto_capture(user_query="x", bot_response="data 無し", matched_category="no_data")
    assert mod.add_comment(fid, "明日整備する")
    items = mod.list_active()
    assert len(items[0]["comments"]) == 1


def test_summary_by_category(tmp_brain):
    mod = tmp_brain
    # NOTE: normalize_query は 数字を mask するので q1/q2 等は同視されてしまう。
    # 別 query にしたい時は数字以外で差を作る。
    mod.auto_capture(user_query="売上は?", bot_response="r1", matched_category="no_data")
    mod.auto_capture(user_query="売上は?", bot_response="r1", matched_category="no_data")  # dedupe
    mod.auto_capture(user_query="客数は?", bot_response="r2", matched_category="no_data")
    mod.auto_capture(user_query="客単価は?", bot_response="r3", matched_category="dunno")
    s = mod.summary_by_category()
    assert "no_data" in s
    assert s["no_data"]["pending"] == 2  # 2 distinct records
    assert s["no_data"]["occurrences"] == 3  # 2 + 1 (= 売上 incremented + 客数)
    assert s["dunno"]["pending"] == 1


# ─── dashboard tests ─────────────────────────────────
def test_nav_includes_data_gaps():
    from services.review_dashboard import _nav
    html = _nav("/admin/review/learning", "test-token")
    assert "/admin/review/data-gaps" in html
    assert "データ拡充" in html


def test_render_data_gaps_page_no_data(tmp_brain):
    from services.review_dashboard import render_data_gaps_page
    html = render_data_gaps_page("test-token")
    assert "データ拡充" in html
    assert "data_gap_detector" in html or "active な gap" in html


def test_render_data_gaps_page_with_data(tmp_brain):
    mod = tmp_brain
    mod.auto_capture(
        user_query="武蔵小山の去年の客単価は?",
        bot_response="データがないです",
        matched_category="no_data",
        matched_text="データがない",
    )
    from services.review_dashboard import render_data_gaps_page
    html = render_data_gaps_page("test-token")
    assert "武蔵小山" in html
    assert "no_data" in html
    assert 'name="action" value="planned"' in html


def test_handle_data_gap_action_planned(tmp_brain):
    mod = tmp_brain
    fid = mod.auto_capture(user_query="x", bot_response="data 無し", matched_category="no_data")
    from services.review_dashboard import handle_data_gap_action
    ok, msg = handle_data_gap_action("planned", fid, note="今週整備")
    assert ok
    assert "planned" in msg


def test_handle_data_gap_action_unknown(tmp_brain):
    from services.review_dashboard import handle_data_gap_action
    ok, msg = handle_data_gap_action("destroy", "any")
    assert not ok


def test_prompt_rule_includes_forward_looking_wording():
    """brain_wiki.py の CLONE_PUBLIC_PROMPT に「今後拡充予定」 wording が含まれる"""
    src = (REPO_ROOT / "brain_wiki.py").read_text(encoding="utf-8")
    # 2a section に新文言が入ってる
    assert "今後拡充予定" in src or "今後集めて少しずつ更新" in src
    assert "データ拡充候補に上げ" in src or "データ拡充候補" in src
