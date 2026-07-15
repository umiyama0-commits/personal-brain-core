"""tests/test_conversation_success.py — 会話継続 = positive signal capture loop

★2026-05-26 海山指示「会話が続いた turn を正解として system 改善に反映する loop」.
"""
from __future__ import annotations

import importlib
import json
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

JST = timezone(timedelta(hours=9))


@pytest.fixture
def tmp_brain(tmp_path, monkeypatch):
    monkeypatch.setenv("BRAIN_ROOT", str(tmp_path))
    if "services.conversation_success" in sys.modules:
        del sys.modules["services.conversation_success"]
    mod = importlib.import_module("services.conversation_success")
    mod.SUCCESS_DIR = tmp_path / "clone_review"
    mod.SUCCESS_FILE = mod.SUCCESS_DIR / "conversation_success.jsonl"
    return mod


# ─── detect_continuation ─────────
def test_detect_continuation_basic(tmp_brain):
    mod = tmp_brain
    now = datetime.now(JST).isoformat()
    history = [
        {"role": "user", "content": "武蔵小山の予算は?", "timestamp": now},
        {"role": "assistant", "content": "予算 5M 円", "timestamp": now},
    ]
    r = mod.detect_continuation("u", "もう少し詳しく", history)
    assert r is not None
    assert r["user_query"] == "武蔵小山の予算は?"
    assert r["bot_response"] == "予算 5M 円"
    assert r["continuation"] == "もう少し詳しく"


def test_detect_skips_correction(tmp_brain):
    mod = tmp_brain
    now = datetime.now(JST).isoformat()
    history = [
        {"role": "user", "content": "q", "timestamp": now},
        {"role": "assistant", "content": "a", "timestamp": now},
    ]
    for correction in ("違う", "そうじゃない", "間違ってる", "正しくは A です"):
        assert mod.detect_continuation("u", correction, history) is None


def test_detect_skips_when_no_bot_turn(tmp_brain):
    mod = tmp_brain
    history = [
        {"role": "user", "content": "first message", "timestamp": datetime.now(JST).isoformat()},
    ]
    assert mod.detect_continuation("u", "follow up", history) is None


def test_detect_skips_when_too_old(tmp_brain):
    mod = tmp_brain
    old = (datetime.now(JST) - timedelta(hours=2)).isoformat()
    history = [
        {"role": "user", "content": "q", "timestamp": old},
        {"role": "assistant", "content": "a", "timestamp": old},
    ]
    # default MAX_CONTINUATION_SECONDS = 30 min
    assert mod.detect_continuation("u", "follow up", history) is None


def test_detect_within_window(tmp_brain):
    mod = tmp_brain
    recent = (datetime.now(JST) - timedelta(minutes=5)).isoformat()
    history = [
        {"role": "user", "content": "q", "timestamp": recent},
        {"role": "assistant", "content": "a", "timestamp": recent},
    ]
    r = mod.detect_continuation("u", "follow up", history)
    assert r is not None
    assert r["elapsed_seconds"] is not None
    assert 0 < r["elapsed_seconds"] < 600


def test_detect_no_history(tmp_brain):
    mod = tmp_brain
    assert mod.detect_continuation("u", "follow up", []) is None
    assert mod.detect_continuation("u", "", [{"role": "assistant", "content": "a", "timestamp": datetime.now(JST).isoformat()}]) is None


# ─── record_success + list / stats ─────────
def test_record_and_list(tmp_brain):
    mod = tmp_brain
    fid = mod.record_success(
        "u", "dm",
        "user_query_text", "bot response text", "continuation text",
        elapsed_seconds=150,
    )
    assert fid.startswith("cont_")
    items = mod.list_recent()
    assert len(items) == 1
    assert items[0]["status"] == "captured"
    assert items[0]["elapsed_seconds"] == 150


def test_count_recent_days(tmp_brain):
    mod = tmp_brain
    mod.record_success("u", "dm", "q1", "a1", "c1")
    mod.record_success("u", "dm", "q2", "a2", "c2")
    assert mod.count_recent_days(7) == 2


def test_update_status(tmp_brain):
    mod = tmp_brain
    fid = mod.record_success("u", "dm", "q", "a", "c")
    assert mod.update_status(fid, "applied")
    items = mod.list_recent()
    assert items[0]["status"] == "applied"

    assert not mod.update_status(fid, "garbage")  # invalid status
    assert not mod.update_status("nonexistent", "applied")


def test_summary_stats(tmp_brain):
    mod = tmp_brain
    mod.record_success("u", "dm", "q1", "a1", "c1")
    mod.record_success("u", "dm", "q2", "a2", "c2")
    fid3 = mod.record_success("u", "dm", "q3", "a3", "c3")
    mod.update_status(fid3, "applied")

    s = mod.summary_stats()
    assert s["total"] == 3
    assert s["by_status"]["captured"] == 2
    assert s["by_status"]["applied"] == 1


# ─── dashboard test ─────────
def test_nav_includes_conversation_success_link():
    from services.review_dashboard import _nav
    html = _nav("/admin/review/learning", "test-token")
    assert "/admin/review/conversation-success" in html
    assert "成功事例" in html


def test_render_conversation_success_page_no_data(tmp_brain):
    from services.review_dashboard import render_conversation_success_page
    html = render_conversation_success_page("test-token")
    assert "成功事例" in html
    assert "正解 dataset" in html or "positive signal" in html


def test_render_conversation_success_page_with_data(tmp_brain):
    mod = tmp_brain
    mod.record_success(
        "user_001", "dm",
        "武蔵小山の予算は?", "予算 5M です",
        "もう少し詳しく", elapsed_seconds=120,
    )
    from services.review_dashboard import render_conversation_success_page
    html = render_conversation_success_page("test-token")
    assert "武蔵小山の予算は?" in html
    assert "予算 5M です" in html
    assert "もう少し詳しく" in html
    assert 'name="action" value="applied"' in html


def test_handle_action_applied(tmp_brain):
    mod = tmp_brain
    fid = mod.record_success("u", "dm", "q", "a", "c")
    from services.review_dashboard import handle_conversation_success_action
    ok, msg = handle_conversation_success_action("applied", fid)
    assert ok
    assert "applied" in msg


def test_handle_action_invalid(tmp_brain):
    from services.review_dashboard import handle_conversation_success_action
    ok, msg = handle_conversation_success_action("destroy", "any")
    assert not ok
    assert "unknown" in msg


def test_handle_action_missing_id(tmp_brain):
    from services.review_dashboard import handle_conversation_success_action
    ok, msg = handle_conversation_success_action("applied", "")
    assert not ok
