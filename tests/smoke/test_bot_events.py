"""smoke test: bot_events.py + bot_metrics.py (bot logging 構造化)。

実 LLM/Bot を呼ばず、log_bot_event / bot_run_context / metrics 集計の
ピュアロジックだけ検証。
"""
from __future__ import annotations

import importlib
import json
import sys
import time
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(REPO_ROOT / "scripts"))


@pytest.fixture
def isolated_bot_events(tmp_path, monkeypatch):
    """BRAIN_ROOT を tmp_path に向け、bot_events を reload してから返す。"""
    brain_root = tmp_path / "data" / "brain"
    brain_root.mkdir(parents=True, exist_ok=True)
    monkeypatch.setenv("BRAIN_ROOT", str(brain_root))
    if "bot_events" in sys.modules:
        importlib.reload(sys.modules["bot_events"])
    import bot_events  # type: ignore
    return bot_events, brain_root


@pytest.mark.smoke
def test_log_bot_event_writes_jsonl(isolated_bot_events):
    """log_bot_event が events.jsonl に 1 行追記する。"""
    bot_events, brain_root = isolated_bot_events
    bot_events.log_bot_event(
        "clone_respond", "turn_finished",
        user_id="abc", model="smart", elapsed_ms=1234, response_chars=120,
    )

    log_path = brain_root / "bot_events" / "events.jsonl"
    assert log_path.exists()
    lines = log_path.read_text(encoding="utf-8").splitlines()
    assert len(lines) == 1
    rec = json.loads(lines[0])
    assert rec["component"] == "clone_respond"
    assert rec["event"] == "turn_finished"
    assert rec["user_id"] == "abc"
    assert rec["elapsed_ms"] == 1234
    assert "ts" in rec


@pytest.mark.smoke
def test_log_bot_event_multiple_lines(isolated_bot_events):
    """複数 event が順番に追記される。"""
    bot_events, brain_root = isolated_bot_events
    bot_events.log_bot_event("comp_a", "ev1", x=1)
    bot_events.log_bot_event("comp_a", "ev2", x=2)
    bot_events.log_bot_event("comp_b", "ev1", x=3)

    log_path = brain_root / "bot_events" / "events.jsonl"
    lines = log_path.read_text(encoding="utf-8").splitlines()
    assert len(lines) == 3
    recs = [json.loads(l) for l in lines]
    assert [r["x"] for r in recs] == [1, 2, 3]


@pytest.mark.smoke
def test_bot_run_context_success(isolated_bot_events):
    """成功時に turn_started + turn_finished の 2 event。"""
    bot_events, brain_root = isolated_bot_events
    with bot_events.bot_run_context("comp", user_id="u1") as ctx:
        ctx["response_chars"] = 100

    log_path = brain_root / "bot_events" / "events.jsonl"
    lines = log_path.read_text(encoding="utf-8").splitlines()
    assert len(lines) == 2
    started, finished = json.loads(lines[0]), json.loads(lines[1])
    assert started["event"] == "turn_started"
    assert finished["event"] == "turn_finished"
    assert finished["response_chars"] == 100
    assert "elapsed_ms" in finished
    # ctx に追加した fields が finished に含まれる
    assert finished["user_id"] == "u1"


@pytest.mark.smoke
def test_bot_run_context_failure(isolated_bot_events):
    """例外時に turn_started + turn_failed、例外は再 raise される。"""
    bot_events, brain_root = isolated_bot_events
    with pytest.raises(ValueError):
        with bot_events.bot_run_context("comp", user_id="u1"):
            raise ValueError("boom")

    log_path = brain_root / "bot_events" / "events.jsonl"
    lines = log_path.read_text(encoding="utf-8").splitlines()
    assert len(lines) == 2
    started, failed = json.loads(lines[0]), json.loads(lines[1])
    assert started["event"] == "turn_started"
    assert failed["event"] == "turn_failed"
    assert failed["error_class"] == "ValueError"
    assert "boom" in failed["error_msg"]


@pytest.mark.smoke
def test_iter_events_filter(isolated_bot_events):
    """iter_events が全 event を yield する (since=None)。"""
    bot_events, _ = isolated_bot_events
    bot_events.log_bot_event("c", "ev1", x=1)
    bot_events.log_bot_event("c", "ev2", x=2)

    events = list(bot_events.iter_events(since_sec=None))
    assert len(events) == 2
    assert events[0]["x"] == 1
    assert events[1]["x"] == 2


@pytest.mark.smoke
def test_iter_events_skips_old(isolated_bot_events, monkeypatch):
    """since_sec で古い event は skip される。"""
    bot_events, brain_root = isolated_bot_events
    log_path = brain_root / "bot_events" / "events.jsonl"
    log_path.parent.mkdir(parents=True, exist_ok=True)
    # 1 件: 1 時間前
    old_rec = {"ts": "2020-01-01T00:00:00", "component": "c", "event": "ev1"}
    # 1 件: 今
    from datetime import datetime
    new_rec = {"ts": datetime.now().isoformat(timespec="seconds"), "component": "c", "event": "ev2"}
    log_path.write_text(json.dumps(old_rec) + "\n" + json.dumps(new_rec) + "\n", encoding="utf-8")

    # 直近 60 秒だけ → new だけ拾う
    events = list(bot_events.iter_events(since_sec=60))
    assert len(events) == 1
    assert events[0]["event"] == "ev2"


@pytest.mark.smoke
def test_parse_since_units(isolated_bot_events):
    bot_events, _ = isolated_bot_events
    assert bot_events.parse_since("7d") == 7 * 86400
    assert bot_events.parse_since("24h") == 24 * 3600
    assert bot_events.parse_since("30m") == 30 * 60
    assert bot_events.parse_since("300s") == 300


@pytest.mark.smoke
def test_log_event_does_not_crash_on_unwritable(isolated_bot_events, monkeypatch):
    """log dir が書けない (parent file 衝突等) でも crash しない。"""
    bot_events, brain_root = isolated_bot_events
    # bot_events ディレクトリと同名のファイルを作って mkdir を失敗させる
    conflict = brain_root / "bot_events_conflict"
    conflict.write_text("blocker", encoding="utf-8")
    monkeypatch.setenv("BRAIN_ROOT", str(conflict))
    importlib.reload(bot_events)

    # crash しない (warning に逃げる)
    bot_events.log_bot_event("comp", "ev", x=1)


# ─── bot_metrics CLI 側 ────────────────────────────
@pytest.mark.smoke
def test_bot_metrics_summary_with_seed(isolated_bot_events):
    """bot_metrics._format_summary が turn_finished/turn_failed を集計できる。"""
    bot_events, _ = isolated_bot_events
    if "bot_metrics" in sys.modules:
        importlib.reload(sys.modules["bot_metrics"])
    import bot_metrics  # type: ignore

    bot_events.log_bot_event("clone_respond", "turn_started", user_id="u1", model="smart")
    bot_events.log_bot_event("clone_respond", "turn_finished",
                              user_id="u1", model="smart", elapsed_ms=1000,
                              response_chars=100, status="ok")
    bot_events.log_bot_event("clone_respond", "turn_started", user_id="u2", model="smart")
    bot_events.log_bot_event("clone_respond", "turn_finished",
                              user_id="u2", model="smart", elapsed_ms=3000,
                              response_chars=200, status="ok")
    bot_events.log_bot_event("clone_memory_update", "turn_started", user_id="u1")
    bot_events.log_bot_event("clone_memory_update", "turn_failed",
                              user_id="u1", elapsed_ms=500, error_class="TimeoutError")

    events = list(bot_events.iter_events())
    out = bot_metrics._format_summary(events, component_filter=None)
    assert "clone_respond" in out
    assert "clone_memory_update" in out
    # クローン応答 2 件 ok、メモリー更新 1 件 fail
    # 行数チェック (header + sep + 2 行 + 警告ブロック)
    assert "total failures in window: 1" in out


@pytest.mark.smoke
def test_bot_metrics_by_user(isolated_bot_events):
    """--by-user で user 別 ranking が出る。"""
    bot_events, _ = isolated_bot_events
    if "bot_metrics" in sys.modules:
        importlib.reload(sys.modules["bot_metrics"])
    import bot_metrics  # type: ignore

    # u_heavy が 3 回、u_light が 1 回
    for _ in range(3):
        bot_events.log_bot_event("clone_respond", "turn_finished",
                                  user_id="u_heavy", elapsed_ms=1000)
    bot_events.log_bot_event("clone_respond", "turn_finished",
                              user_id="u_light", elapsed_ms=2000)

    events = list(bot_events.iter_events())
    out = bot_metrics._format_by_user(events)
    # heavy が先頭
    lines = out.splitlines()
    # ヘッダ + sep + 2 user 行
    data_lines = [l for l in lines if "u_heavy" in l or "u_light" in l]
    assert data_lines[0].startswith("u_heavy"), f"heavy が先頭にないと assert fail: {out}"


@pytest.mark.smoke
def test_bot_metrics_failures(isolated_bot_events):
    bot_events, _ = isolated_bot_events
    if "bot_metrics" in sys.modules:
        importlib.reload(sys.modules["bot_metrics"])
    import bot_metrics  # type: ignore

    bot_events.log_bot_event("clone_respond", "turn_finished",
                              user_id="u1", elapsed_ms=1000)
    bot_events.log_bot_event("clone_respond", "turn_failed",
                              user_id="u1", elapsed_ms=500,
                              error_class="TimeoutError")

    events = list(bot_events.iter_events())
    out = bot_metrics._format_failures(events, component_filter=None)
    assert "TimeoutError" in out
    assert "clone_respond" in out


@pytest.mark.smoke
def test_bot_metrics_no_events(isolated_bot_events):
    """events 0 件で crash しない、（no events ...）と返る。"""
    bot_events, _ = isolated_bot_events
    if "bot_metrics" in sys.modules:
        importlib.reload(sys.modules["bot_metrics"])
    import bot_metrics  # type: ignore

    out = bot_metrics._format_summary([], component_filter=None)
    assert "no events" in out
