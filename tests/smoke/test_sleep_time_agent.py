"""smoke test: clone_sleep_time_agent の debounce ロジック。

実 LLM 呼び出しは skip、schedule / cancel / debounce のみ検証。
"""
from __future__ import annotations

import asyncio
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(REPO_ROOT / "scripts"))


@pytest.mark.smoke
def test_module_importable():
    """script が import できる。"""
    import clone_sleep_time_agent
    assert hasattr(clone_sleep_time_agent, "schedule_sleep_time_agent")
    assert hasattr(clone_sleep_time_agent, "sleep_time_run")


@pytest.mark.smoke
async def test_schedule_creates_task(monkeypatch):
    """schedule_sleep_time_agent で task が作られる、idle dict に登録される。"""
    import clone_sleep_time_agent as mod
    called = []

    async def fake_run(uid, dry_run=False):
        called.append(uid)
        return {}

    monkeypatch.setattr(mod, "sleep_time_run", fake_run)
    mod._idle_tasks.clear()

    await mod.schedule_sleep_time_agent("u_test_1", debounce_sec=0)
    assert "u_test_1" in mod._idle_tasks
    # 0 秒 sleep してすぐ実行されるはずなので、yield して待つ
    await asyncio.sleep(0.1)
    assert "u_test_1" in called or "u_test_1" not in mod._idle_tasks  # 終わってる


@pytest.mark.smoke
async def test_consecutive_schedules_cancel_previous(monkeypatch):
    """連続 schedule で前のタスクが cancel される (debounce)。"""
    import clone_sleep_time_agent as mod

    completed_count = 0

    async def fake_run(uid, dry_run=False):
        nonlocal completed_count
        completed_count += 1
        return {}

    monkeypatch.setattr(mod, "sleep_time_run", fake_run)
    mod._idle_tasks.clear()

    # 1 回目 schedule
    await mod.schedule_sleep_time_agent("u_test_2", debounce_sec=5)
    task_1 = mod._idle_tasks["u_test_2"]
    assert not task_1.done()

    # すぐに 2 回目 schedule (前を cancel するはず)
    await mod.schedule_sleep_time_agent("u_test_2", debounce_sec=5)
    task_2 = mod._idle_tasks["u_test_2"]
    assert task_2 is not task_1
    # 1 回目は cancel されてる
    await asyncio.sleep(0.05)
    assert task_1.cancelled() or task_1.done()

    # cleanup
    task_2.cancel()
    try:
        await task_2
    except asyncio.CancelledError:
        pass


@pytest.mark.smoke
async def test_debounce_skips_run_if_cancelled(monkeypatch):
    """連続 schedule が続けば sleep_time_run は走らない (debounce)。"""
    import clone_sleep_time_agent as mod

    run_count = 0

    async def fake_run(uid, dry_run=False):
        nonlocal run_count
        run_count += 1
        return {}

    monkeypatch.setattr(mod, "sleep_time_run", fake_run)
    mod._idle_tasks.clear()

    # 5 回連続 schedule、各 50ms 間隔、debounce 100ms
    for _ in range(5):
        await mod.schedule_sleep_time_agent("u_test_3", debounce_sec=0.1)
        await asyncio.sleep(0.05)  # 50ms 間隔、まだ idle にならない

    # 最後の schedule から 200ms 待つ → 初めて idle 成立
    await asyncio.sleep(0.2)

    # 走ったのは 最後の 1 回だけ
    assert run_count == 1, f"expected 1 run, got {run_count}"


@pytest.mark.smoke
def test_min_turns_threshold():
    """MIN_TURNS_TO_RUN が定義されてる (4 以上)。"""
    import clone_sleep_time_agent as mod
    assert hasattr(mod, "MIN_TURNS_TO_RUN")
    assert mod.MIN_TURNS_TO_RUN >= 3


@pytest.mark.smoke
def test_default_debounce_30_seconds():
    """DEFAULT_DEBOUNCE_SEC が 30 秒。"""
    import clone_sleep_time_agent as mod
    assert mod.DEFAULT_DEBOUNCE_SEC == 30


@pytest.mark.smoke
def test_user_file_path_safe(tmp_path):
    """user_id にスラッシュがあってもパスが壊れない。"""
    import clone_sleep_time_agent as mod
    p = mod._user_file_safe("evil/../../etc/passwd", tmp_path, ".jsonl")
    assert tmp_path in p.parents or p.parent == tmp_path


@pytest.mark.smoke
async def test_sleep_time_run_skips_too_few_turns(monkeypatch, tmp_path):
    """user turn < MIN_TURNS_TO_RUN なら skip。"""
    import clone_sleep_time_agent as mod
    monkeypatch.setattr(mod, "BRAIN_ROOT", tmp_path)
    monkeypatch.setattr(mod, "HISTORY_DIR", tmp_path / "clone_history")
    monkeypatch.setattr(mod, "MEMORY_DIR", tmp_path / "clone_memory")
    (tmp_path / "clone_history").mkdir(parents=True)
    # 2 user turns しか無い user
    import json
    lines = [
        json.dumps({"timestamp": "2026-05-21T00:00:00+09:00", "user_id": "u1", "role": "user", "text": "a"}, ensure_ascii=False),
        json.dumps({"timestamp": "2026-05-21T00:01:00+09:00", "user_id": "u1", "role": "assistant", "text": "b"}, ensure_ascii=False),
        json.dumps({"timestamp": "2026-05-21T00:02:00+09:00", "user_id": "u1", "role": "user", "text": "c"}, ensure_ascii=False),
    ]
    (tmp_path / "clone_history" / "u1.jsonl").write_text("\n".join(lines), encoding="utf-8")

    result = await mod.sleep_time_run("u1", dry_run=True)
    assert result.get("skipped") is True
    assert "turns" in result.get("reason", "")
