"""smoke test: tasks/self_improve.py (★2026-05-22 Phase 4)。

state file の read/write が tmp 環境で動くか + main.py の wrapper 確認。
self_improve_loop 本体は LiteLLM 接続が必要なので integration 扱い (= smoke では skip)。
"""
from __future__ import annotations

import os
from pathlib import Path

import pytest


@pytest.mark.smoke
def test_state_file_read_write(tmp_path, monkeypatch):
    """SELF_IMPROVE_STATE_FILE が tmp に向くよう env で override → read/write 動作。"""
    state_file = tmp_path / "self_improve_last_run.txt"
    monkeypatch.setenv("SELF_IMPROVE_STATE_FILE", str(state_file))
    # module 再 import で env を反映
    import importlib
    import tasks.self_improve as ts
    importlib.reload(ts)

    # 初期は 0.0
    assert ts.read_last_self_improve_ts() == 0.0
    # 書き込み → 読み出し
    ts.write_last_self_improve_ts(1234567890.5)
    assert ts.read_last_self_improve_ts() == 1234567890.5
    # ファイル実在確認
    assert state_file.exists()
    assert state_file.read_text().strip() == "1234567890.5"


@pytest.mark.smoke
def test_state_file_handles_missing(tmp_path, monkeypatch):
    """state file が無い場合に 0.0 を返す。"""
    state_file = tmp_path / "nope.txt"
    monkeypatch.setenv("SELF_IMPROVE_STATE_FILE", str(state_file))
    import importlib
    import tasks.self_improve as ts
    importlib.reload(ts)

    assert not state_file.exists()
    assert ts.read_last_self_improve_ts() == 0.0


@pytest.mark.smoke
def test_state_file_handles_garbage(tmp_path, monkeypatch):
    """state file に garbage が書かれてても 0.0 fallback。"""
    state_file = tmp_path / "garbage.txt"
    state_file.write_text("not-a-float-xyz")
    monkeypatch.setenv("SELF_IMPROVE_STATE_FILE", str(state_file))
    import importlib
    import tasks.self_improve as ts
    importlib.reload(ts)

    assert ts.read_last_self_improve_ts() == 0.0


@pytest.mark.smoke
def test_main_py_wraps_tasks_self_improve():
    """main.py が tasks.self_improve から import している (= 切り出し成功)。"""
    REPO = Path(__file__).resolve().parent.parent.parent
    src = (REPO / "main.py").read_text(encoding="utf-8")
    assert "from tasks.self_improve import" in src
    assert "_self_improve_loop_impl" in src
    assert "push_message_fn=push_message" in src
    # 旧 inline 定義は消えてる
    assert "def _read_last_self_improve_ts() -> float:" not in src
    assert "def _write_last_self_improve_ts(ts: float)" not in src


@pytest.mark.smoke
def test_tasks_init_py_exists():
    REPO = Path(__file__).resolve().parent.parent.parent
    init = REPO / "tasks" / "__init__.py"
    assert init.exists()
    assert "Phase 4" in init.read_text(encoding="utf-8")
