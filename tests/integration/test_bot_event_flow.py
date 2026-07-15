"""
Integration test: bot_events + bot_run_context が 1 turn fully に動く。

実 LLM / Chroma 不要。conftest.py の MockHTTPClient で代用。
"""
from __future__ import annotations

import asyncio
import importlib
import json
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(REPO_ROOT / "scripts"))


@pytest.mark.integration
def test_bot_run_context_records_full_turn(isolated_brain_root):
    """bot_run_context が turn_started + turn_finished を JSON で書き出す。"""
    if "bot_events" in sys.modules:
        importlib.reload(sys.modules["bot_events"])
    import bot_events  # type: ignore

    with bot_events.bot_run_context(
        "clone_respond",
        user_id="alice123",
        model="smart",
        query_chars=10,
        wiki_chars=50000,
        has_memory=True,
        bucket="A",
        experiment_id="exp-001",
    ) as ctx:
        # 応答処理 simulation
        ctx["response_chars"] = 420
        ctx["status"] = "ok"

    log_path = isolated_brain_root / "bot_events" / "events.jsonl"
    assert log_path.exists()
    lines = log_path.read_text(encoding="utf-8").splitlines()
    assert len(lines) == 2
    started = json.loads(lines[0])
    finished = json.loads(lines[1])
    assert started["event"] == "turn_started"
    assert finished["event"] == "turn_finished"
    assert finished["bucket"] == "A"
    assert finished["experiment_id"] == "exp-001"
    assert finished["response_chars"] == 420
    assert finished["elapsed_ms"] >= 0


@pytest.mark.integration
def test_bot_run_context_failure_records_error(isolated_brain_root):
    """turn 内で例外発生時に turn_failed + error_class が記録される。"""
    if "bot_events" in sys.modules:
        importlib.reload(sys.modules["bot_events"])
    import bot_events  # type: ignore

    with pytest.raises(RuntimeError):
        with bot_events.bot_run_context("clone_respond", user_id="bob",
                                         model="smart") as ctx:
            ctx["wiki_chars"] = 1000
            raise RuntimeError("LLM timeout")

    log_path = isolated_brain_root / "bot_events" / "events.jsonl"
    lines = log_path.read_text(encoding="utf-8").splitlines()
    failed = json.loads(lines[-1])
    assert failed["event"] == "turn_failed"
    assert failed["error_class"] == "RuntimeError"
    assert "LLM timeout" in failed["error_msg"]
    assert failed["wiki_chars"] == 1000


@pytest.mark.integration
def test_ab_bucket_assigned_in_event(isolated_brain_root):
    """A/B 実験 active 時、turn_finished に bucket / experiment_id が含まれる。"""
    if "bot_events" in sys.modules:
        importlib.reload(sys.modules["bot_events"])
    if "clone_ab_test" in sys.modules:
        importlib.reload(sys.modules["clone_ab_test"])
    import bot_events  # type: ignore
    import clone_ab_test as ab  # type: ignore

    ab.create_experiment("exp-bot-1", {"model": "smart"}, {"model": "smart-gpt"})

    # user A の bucket を取得
    cfg = ab.get_bucket_config("test_user_5", "exp-bot-1")
    assert cfg["bucket"] in ("A", "B")

    # bot_run_context にその bucket を渡す
    with bot_events.bot_run_context(
        "clone_respond",
        user_id="test_user_5",
        model=cfg["model"],
        bucket=cfg["bucket"],
        experiment_id=cfg["experiment_id"],
    ) as ctx:
        ctx["response_chars"] = 100

    log_path = isolated_brain_root / "bot_events" / "events.jsonl"
    lines = log_path.read_text(encoding="utf-8").splitlines()
    finished = json.loads(lines[-1])
    assert finished["bucket"] in ("A", "B")
    assert finished["experiment_id"] == "exp-bot-1"


@pytest.mark.integration
def test_ab_analyze_reads_bot_events(isolated_brain_root):
    """clone_ab_test.analyze_experiment が bot_events.jsonl を読んで集計する。"""
    if "bot_events" in sys.modules:
        importlib.reload(sys.modules["bot_events"])
    if "clone_ab_test" in sys.modules:
        importlib.reload(sys.modules["clone_ab_test"])
    import bot_events  # type: ignore
    import clone_ab_test as ab  # type: ignore

    ab.create_experiment("exp-analyze", {"model": "smart"}, {"model": "smart-gpt"})

    # 5 turn × A, 5 turn × B のイベント seed
    for i in range(5):
        with bot_events.bot_run_context(
            "clone_respond",
            user_id=f"u_a_{i}", model="smart",
            bucket="A", experiment_id="exp-analyze",
        ) as ctx:
            ctx["response_chars"] = 100 + i
    for i in range(5):
        with bot_events.bot_run_context(
            "clone_respond",
            user_id=f"u_b_{i}", model="smart-gpt",
            bucket="B", experiment_id="exp-analyze",
        ) as ctx:
            ctx["response_chars"] = 200 + i

    report = ab.analyze_experiment("exp-analyze", days=1)
    assert report["bucket_a"]["n_finished"] == 5
    assert report["bucket_b"]["n_finished"] == 5
    assert report["bucket_a"]["response_chars"]["mean"] < report["bucket_b"]["response_chars"]["mean"]


@pytest.mark.integration
async def test_async_run_context_works(isolated_brain_root):
    """async 内でも bot_run_context が動く。"""
    if "bot_events" in sys.modules:
        importlib.reload(sys.modules["bot_events"])
    import bot_events  # type: ignore

    async def do_work():
        with bot_events.bot_run_context("clone_respond_async",
                                         user_id="async_u") as ctx:
            await asyncio.sleep(0.01)
            ctx["response_chars"] = 99
            return "ok"

    result = await do_work()
    assert result == "ok"
    log_path = isolated_brain_root / "bot_events" / "events.jsonl"
    lines = log_path.read_text(encoding="utf-8").splitlines()
    assert len(lines) == 2
    finished = json.loads(lines[-1])
    assert finished["response_chars"] == 99
    assert finished["elapsed_ms"] >= 10
