"""tests/test_dashboard_three_fixes.py — 海山指示 3 改善:
1. 日次回答数 を 数字で見える化 (top page table)
2. 要 attention の resolve button + audit_stats で除外
3. Memory page を最新回答順 sort + user_id 永続 alias
"""
from __future__ import annotations

import importlib
import json
import sys
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

JST = timezone(timedelta(hours=9))


# ─── Fix 1: audit needs_attention resolve ──────────
@pytest.fixture
def tmp_brain_audit(tmp_path, monkeypatch):
    monkeypatch.setenv("BRAIN_ROOT", str(tmp_path))
    if "clone_audit" in sys.modules:
        del sys.modules["clone_audit"]
    mod = importlib.import_module("clone_audit")
    mod.AUDIT_DIR = tmp_path / "clone_audit"
    mod.AUDIT_DIR.mkdir(parents=True, exist_ok=True)
    return mod


def test_audit_stats_excludes_resolved(tmp_brain_audit):
    """audit_stats() で resolved=True の bad/fix record は needs_attention に含まれない"""
    mod = tmp_brain_audit
    today = datetime.now().date()
    f = mod.AUDIT_DIR / f"{today.isoformat()}.jsonl"

    records = [
        {"id": "r1", "verdict": "bad", "user_query": "x1", "bot_response": "y1",
         "timestamp": datetime.now().isoformat(), "note": "未解決"},
        {"id": "r2", "verdict": "bad", "user_query": "x2", "bot_response": "y2",
         "timestamp": datetime.now().isoformat(), "note": "解決済", "resolved": True},
        {"id": "r3", "verdict": "fix", "user_query": "x3", "bot_response": "y3",
         "timestamp": datetime.now().isoformat(), "note": "未対応"},
        {"id": "r4", "verdict": "good", "user_query": "x4", "bot_response": "y4",
         "timestamp": datetime.now().isoformat()},  # good = 元から除外
    ]
    f.write_text("\n".join(json.dumps(r, ensure_ascii=False) for r in records), encoding="utf-8")

    stats = mod.audit_stats(days=30)
    needs_ids = [r["id"] for r in stats["needs_attention"]]
    assert "r1" in needs_ids  # bad, unresolved
    assert "r3" in needs_ids  # fix, unresolved
    assert "r2" not in needs_ids  # bad だが resolved=True
    assert "r4" not in needs_ids  # good
    assert stats["n_bad"] == 2  # bad count は resolved 含めて 2 件
    assert stats["n_fix"] == 1


def test_mark_resolved_basic(tmp_brain_audit):
    mod = tmp_brain_audit
    today = datetime.now().date()
    f = mod.AUDIT_DIR / f"{today.isoformat()}.jsonl"
    rec = {"id": "rec1", "verdict": "bad", "note": "原因不明"}
    f.write_text(json.dumps(rec, ensure_ascii=False), encoding="utf-8")

    ok = mod.mark_resolved("rec1", resolved_by="umiyama", note="store-yoy 拡張で対応済")
    assert ok

    loaded = json.loads(f.read_text(encoding="utf-8").strip())
    assert loaded["resolved"] is True
    assert loaded["resolved_by"] == "umiyama"
    assert "store-yoy 拡張で対応済" in loaded["note"]
    assert "resolved_at" in loaded


def test_mark_resolved_missing(tmp_brain_audit):
    mod = tmp_brain_audit
    assert not mod.mark_resolved("nonexistent")
    assert not mod.mark_resolved("")


def test_handle_audit_action_resolve(tmp_brain_audit, monkeypatch):
    mod = tmp_brain_audit
    today = datetime.now().date()
    f = mod.AUDIT_DIR / f"{today.isoformat()}.jsonl"
    f.write_text(json.dumps({"id": "rec99", "verdict": "bad"}), encoding="utf-8")

    from services.review_dashboard import handle_audit_action
    ok, msg = handle_audit_action("resolve", "needs_attention", item_id="rec99")
    assert ok
    assert "resolved" in msg

    # source != needs_attention は拒否
    ok2, msg2 = handle_audit_action("resolve", "unrated", item_id="rec99")
    assert not ok2

    # id 無し
    ok3, msg3 = handle_audit_action("resolve", "needs_attention", item_id="")
    assert not ok3


# ─── Fix 2: Memory sort by latest ──────────
def test_render_memory_page_sort_by_latest(tmp_path, monkeypatch):
    """list_users が複数 user 返す時、last_updated 降順で render される"""
    import services.review_dashboard as rd
    # mock clone_memory.list_users
    fake_users = [
        {"user_id": "user_AAA_old", "turn_count": 10, "size": 1024, "last_updated": "2026-05-01T10:00:00"},
        {"user_id": "user_BBB_newest", "turn_count": 5, "size": 2048, "last_updated": "2026-05-26T10:00:00"},
        {"user_id": "user_CCC_mid", "turn_count": 8, "size": 1500, "last_updated": "2026-05-15T10:00:00"},
    ]
    import clone_memory
    monkeypatch.setattr(clone_memory, "list_users", lambda: fake_users)

    html = rd.render_memory_page("test-token")
    # newest (BBB) が CCC, AAA より先に登場すること
    pos_b = html.find("user_BBB_newest")
    pos_c = html.find("user_CCC_mid")
    pos_a = html.find("user_AAA_old")
    assert pos_b > 0
    assert pos_b < pos_c < pos_a  # 新→旧 順に登場

    # 表示順は最新降順 と explanation 文言が含まれてる
    assert "最新回答" in html or "last_updated" in html


# ─── Fix 3: Daily numeric table on top page ──────────
def test_render_top_page_has_daily_numeric_table(monkeypatch):
    """top page に 日次 回答数 の数字 table が含まれる"""
    import services.review_dashboard as rd
    fake_data = {
        "learning_pending": 0, "feedback_pending": 0, "research_pending": 0,
        "audit_needs_attention": 0, "audit_total_30d": 0, "audit_good_rate": 0,
        "usage": {
            "summary": {"total_queries": 100, "failure_rate_pct": 2.0, "avg_latency_ms": 200, "p95_latency_ms": 500},
            "channel_split": {"dm_count": 80, "group_count": 20, "group_pct": 20},
            "roi_progress": {"progress_pct": 10, "current_pace_estimate_monthly": 1000},
            "daily_trend": [
                {"date": "2026-05-20", "queries": 30, "failures": 1},
                {"date": "2026-05-21", "queries": 45, "failures": 0},
                {"date": "2026-05-22", "queries": 38, "failures": 2},
                {"date": "2026-05-23", "queries": 55, "failures": 1},
                {"date": "2026-05-24", "queries": 42, "failures": 0},
                {"date": "2026-05-25", "queries": 60, "failures": 0},
                {"date": "2026-05-26", "queries": 51, "failures": 3},
            ],
        },
    }
    monkeypatch.setattr(rd, "aggregate_review_queues", lambda: fake_data)

    html = rd.render_top_page("test-token")
    # 数字 table が含まれる
    assert "回答数" in html
    # 各日付の数字 (newest first)
    for q in (30, 45, 38, 55, 42, 60, 51):
        assert str(q) in html
    # fail 数も (= 0 以上のみ)
    assert "fail 1" in html or "fail 2" in html or "fail 3" in html
    # 曜日 label
    assert "(火)" in html or "(水)" in html or "(木)" in html or "(金)" in html


def test_render_top_page_no_data_no_table(monkeypatch):
    """daily_trend 空でも crash しない"""
    import services.review_dashboard as rd
    fake_data = {
        "learning_pending": 0, "feedback_pending": 0, "research_pending": 0,
        "audit_needs_attention": 0,
        "usage": {"summary": {}, "channel_split": {}, "roi_progress": {}, "daily_trend": []},
    }
    monkeypatch.setattr(rd, "aggregate_review_queues", lambda: fake_data)
    html = rd.render_top_page("test-token")
    assert "Review Dashboard" in html or "Dashboard" in html
