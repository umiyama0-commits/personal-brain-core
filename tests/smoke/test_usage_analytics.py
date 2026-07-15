"""smoke test: services/usage_analytics.py (★2026-05-24 Feature 2/4 Usage Dashboard)

bot_events.jsonl から ROI tracking 用 aggregate を構築できるか。
"""
from __future__ import annotations

import importlib
import json
import sys
from datetime import datetime, timezone, timedelta
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(REPO))
sys.path.insert(0, str(REPO / "scripts"))


def _write_events(tmp_path, events):
    """tmp_path/data/brain/bot_events/events.jsonl に events を書き込む。"""
    log_dir = tmp_path / "data" / "brain" / "bot_events"
    log_dir.mkdir(parents=True, exist_ok=True)
    log_file = log_dir / "events.jsonl"
    with log_file.open("w", encoding="utf-8") as f:
        for ev in events:
            f.write(json.dumps(ev, ensure_ascii=False) + "\n")


@pytest.mark.smoke
def test_aggregate_empty(tmp_path, monkeypatch):
    """events 0 件 → summary 全 0、ROI progress 0%。"""
    monkeypatch.setenv("BRAIN_ROOT", str(tmp_path / "data" / "brain"))
    from services.usage_analytics import aggregate_usage
    data = aggregate_usage(since_sec=86400 * 30)
    assert data["summary"]["total_queries"] == 0
    assert data["summary"]["failure_rate_pct"] == 0
    assert data["roi_progress"]["progress_pct"] == 0
    assert data["channel_split"]["dm_count"] == 0
    assert data["channel_split"]["group_count"] == 0


@pytest.mark.smoke
def test_aggregate_basic_query_count(tmp_path, monkeypatch):
    """5 turns finished で total_queries=5、total_replies=5。"""
    monkeypatch.setenv("BRAIN_ROOT", str(tmp_path / "data" / "brain"))
    now = datetime.now()
    events = []
    for i in range(5):
        ts = (now - timedelta(hours=i)).isoformat()
        events.append({"ts": ts, "event": "turn_started", "component": "clone_respond", "user_id": f"u{i}"})
        events.append({"ts": ts, "event": "turn_finished", "component": "clone_respond",
                       "user_id": f"u{i}", "elapsed_ms": 1500 + i * 100})
    _write_events(tmp_path, events)

    from services.usage_analytics import aggregate_usage
    data = aggregate_usage(since_sec=86400 * 30)
    assert data["summary"]["total_queries"] == 5
    assert data["summary"]["total_replies"] == 5
    assert data["summary"]["total_failures"] == 0
    assert data["summary"]["avg_latency_ms"] > 1000


@pytest.mark.smoke
def test_aggregate_failure_rate(tmp_path, monkeypatch):
    """10 started, 3 failed → failure rate 30%。"""
    monkeypatch.setenv("BRAIN_ROOT", str(tmp_path / "data" / "brain"))
    now = datetime.now()
    events = []
    for i in range(10):
        ts = (now - timedelta(hours=i)).isoformat()
        events.append({"ts": ts, "event": "turn_started", "component": "clone_respond", "user_id": f"u{i}"})
        if i < 3:
            events.append({"ts": ts, "event": "turn_failed", "component": "clone_respond",
                           "user_id": f"u{i}", "error_class": "TimeoutError"})
        else:
            events.append({"ts": ts, "event": "turn_finished", "component": "clone_respond",
                           "user_id": f"u{i}", "elapsed_ms": 1500})
    _write_events(tmp_path, events)

    from services.usage_analytics import aggregate_usage
    data = aggregate_usage(since_sec=86400 * 30)
    assert data["summary"]["total_queries"] == 10
    assert data["summary"]["total_failures"] == 3
    assert data["summary"]["failure_rate_pct"] == 30.0


@pytest.mark.smoke
def test_aggregate_channel_split(tmp_path, monkeypatch):
    """DM vs Group split. channel_id 有り = group, 無し = DM。"""
    monkeypatch.setenv("BRAIN_ROOT", str(tmp_path / "data" / "brain"))
    now = datetime.now()
    events = []
    # 3 DM
    for i in range(3):
        events.append({"ts": now.isoformat(), "event": "turn_finished",
                       "component": "clone_respond", "user_id": f"u_dm_{i}", "elapsed_ms": 1500})
    # 2 group
    for i in range(2):
        events.append({"ts": now.isoformat(), "event": "turn_finished",
                       "component": "clone_respond", "user_id": f"u_g_{i}",
                       "channel_id": f"ch_{i}", "elapsed_ms": 1500})
    _write_events(tmp_path, events)

    from services.usage_analytics import aggregate_usage
    data = aggregate_usage(since_sec=86400 * 30)
    assert data["channel_split"]["dm_count"] == 3
    assert data["channel_split"]["group_count"] == 2
    assert data["channel_split"]["group_pct"] == 40.0


@pytest.mark.smoke
def test_aggregate_top_users(tmp_path, monkeypatch):
    """top_users ranking: heavy user 識別。"""
    monkeypatch.setenv("BRAIN_ROOT", str(tmp_path / "data" / "brain"))
    now = datetime.now()
    events = []
    # heavy user u1: 10 turns
    for i in range(10):
        events.append({"ts": now.isoformat(), "event": "turn_finished",
                       "component": "clone_respond", "user_id": "u_heavy", "elapsed_ms": 1500})
    # medium user u2: 3 turns
    for i in range(3):
        events.append({"ts": now.isoformat(), "event": "turn_finished",
                       "component": "clone_respond", "user_id": "u_med", "elapsed_ms": 2000})
    # 1 light user
    events.append({"ts": now.isoformat(), "event": "turn_finished",
                   "component": "clone_respond", "user_id": "u_light", "elapsed_ms": 1000})
    _write_events(tmp_path, events)

    from services.usage_analytics import aggregate_usage
    data = aggregate_usage(since_sec=86400 * 30)
    top = data["top_users"]
    assert top[0]["user_id"] == "u_heavy"
    assert top[0]["turns"] == 10
    assert top[1]["user_id"] == "u_med"
    assert top[2]["user_id"] == "u_light"


@pytest.mark.smoke
def test_aggregate_components_breakdown(tmp_path, monkeypatch):
    """component 別の ok/fail/avg_ms 集計。"""
    monkeypatch.setenv("BRAIN_ROOT", str(tmp_path / "data" / "brain"))
    now = datetime.now()
    events = [
        {"ts": now.isoformat(), "event": "turn_finished", "component": "clone_respond",
         "user_id": "u1", "elapsed_ms": 2000},
        {"ts": now.isoformat(), "event": "turn_finished", "component": "cohere_rerank",
         "user_id": "u1", "elapsed_ms": 250},
        {"ts": now.isoformat(), "event": "turn_failed", "component": "drive_ingest",
         "user_id": "u1", "error_class": "HttpError"},
    ]
    _write_events(tmp_path, events)

    from services.usage_analytics import aggregate_usage
    data = aggregate_usage(since_sec=86400 * 30)
    comps = data["components"]
    assert "clone_respond" in comps
    assert "cohere_rerank" in comps
    assert "drive_ingest" in comps
    assert comps["clone_respond"]["ok"] == 1
    assert comps["cohere_rerank"]["ok"] == 1
    assert comps["drive_ingest"]["fail"] == 1


@pytest.mark.smoke
def test_aggregate_roi_progress(tmp_path, monkeypatch):
    """ROI progress: 30d で 500 queries → 月間 pace 500, progress 50%。"""
    monkeypatch.setenv("BRAIN_ROOT", str(tmp_path / "data" / "brain"))
    now = datetime.now()
    events = []
    for i in range(500):
        events.append({"ts": (now - timedelta(hours=i)).isoformat(),
                       "event": "turn_started", "component": "clone_respond", "user_id": f"u{i % 50}"})
    _write_events(tmp_path, events)

    from services.usage_analytics import aggregate_usage
    data = aggregate_usage(since_sec=86400 * 30)
    roi = data["roi_progress"]
    assert roi["monthly_target"] == 1000
    assert roi["current_pace_estimate_monthly"] == 500
    assert roi["progress_pct"] == 50.0
    assert roi["phase_1_full_target"] == 10000


@pytest.mark.smoke
def test_render_dashboard_html_basic(tmp_path, monkeypatch):
    """HTML dashboard が render 可能 + 主要 section が含まれる。"""
    monkeypatch.setenv("BRAIN_ROOT", str(tmp_path / "data" / "brain"))
    now = datetime.now()
    events = [
        {"ts": now.isoformat(), "event": "turn_started", "component": "clone_respond", "user_id": "u1"},
        {"ts": now.isoformat(), "event": "turn_finished", "component": "clone_respond",
         "user_id": "u1", "elapsed_ms": 1500},
    ]
    _write_events(tmp_path, events)

    from services.usage_analytics import aggregate_usage, render_dashboard_html
    data = aggregate_usage(since_sec=86400 * 7)
    html = render_dashboard_html(data)
    assert "<!DOCTYPE html>" in html
    assert "Phase 1 ROI Progress" in html
    assert "Summary" in html
    assert "Channel Split" in html
    assert "Top Users" in html
    assert "Components" in html
    assert "Daily Trend" in html


@pytest.mark.smoke
def test_window_label_formatting(tmp_path, monkeypatch):
    """window_label の format (= 30 days / 24 hours 等)。"""
    monkeypatch.setenv("BRAIN_ROOT", str(tmp_path / "data" / "brain"))
    from services.usage_analytics import aggregate_usage
    data = aggregate_usage(since_sec=86400 * 30)
    assert "days" in data["window_label"]
    data2 = aggregate_usage(since_sec=86400 * 7)
    assert "days" in data2["window_label"]
    data3 = aggregate_usage(since_sec=3600 * 24)
    # 24h は丁度 1 days
    assert "day" in data3["window_label"] or "hour" in data3["window_label"]


@pytest.mark.smoke
def test_daily_trend_sorted(tmp_path, monkeypatch):
    """daily_trend が日付昇順で sorted。"""
    monkeypatch.setenv("BRAIN_ROOT", str(tmp_path / "data" / "brain"))
    # 過去 5 日に分散
    events = []
    for d in range(5):
        ts = (datetime.now() - timedelta(days=d)).isoformat()
        events.append({"ts": ts, "event": "turn_started", "component": "clone_respond", "user_id": "u1"})
    _write_events(tmp_path, events)

    from services.usage_analytics import aggregate_usage
    data = aggregate_usage(since_sec=86400 * 7)
    trend = data["daily_trend"]
    if len(trend) >= 2:
        dates = [t["date"] for t in trend]
        assert dates == sorted(dates)  # 昇順
