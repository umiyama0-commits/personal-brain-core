"""tests/test_usage_automated_filter.py — daily 利用数から batch/eval を除外する filter。

★2026-06-09 海山指示「バッチ等はアクセスとしてカウントしない」: video-align batch / eval /
synthetic / health / hybrid の自動化呼び出しを daily_trend の queries (実ユーザー利用) から除外し、
automated として別計上する。
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "services"))
import usage_analytics as ua  # noqa: E402


def test_is_automated_user_classification():
    # 自動化 (= 数えない)
    for u in ["", "video_align_local", "synthetic_emp_3", "health_e", "hybrid_1",
              "eval_runner", "test_x", "regression_x"]:
        assert ua._is_automated_user(u) is True, u
    # 実ユーザー (= hex UUID、数える)
    for u in ["2f8f09c8", "51d6757b-1234", "db784aa8", "cea35620", "00a63d1b"]:
        assert ua._is_automated_user(u) is False, u


def test_aggregate_usage_excludes_automated(monkeypatch):
    events = [
        # 実ユーザー clone_respond (2 件)
        {"component": "clone_respond", "event": "turn_finished", "user_id": "2f8f09c8", "ts": "2026-06-08T10:00:00"},
        {"component": "clone_respond", "event": "turn_finished", "user_id": "2f8f09c8", "ts": "2026-06-08T11:00:00"},
        # video batch (除外)
        {"component": "clone_respond", "event": "turn_finished", "user_id": "video_align_local", "ts": "2026-06-08T18:00:00"},
        # eval 空 user_id (除外)
        {"component": "clone_respond", "event": "turn_finished", "user_id": "", "ts": "2026-06-08T03:00:00"},
        # 実ユーザーの失敗 (1 件)
        {"component": "clone_respond", "event": "turn_failed", "user_id": "51d6757b", "ts": "2026-06-08T12:00:00"},
        # 自動化の失敗 (除外)
        {"component": "clone_respond", "event": "turn_failed", "user_id": "video_align_local", "ts": "2026-06-08T18:01:00"},
    ]
    monkeypatch.setattr(ua, "_events_in_window", lambda since_sec=0: events)
    out = ua.aggregate_usage()
    by_date = {d["date"]: d for d in out["daily_trend"]}
    day = by_date["2026-06-08"]
    assert day["queries"] == 2      # 実ユーザーのみ
    assert day["automated"] == 2    # video + 空(eval)
    assert day["failures"] == 1     # 実ユーザーの fail のみ (video の fail は除外)
