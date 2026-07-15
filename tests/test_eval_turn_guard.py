"""eval_turn_guard のコスト保護 (★2026-06-11、6/10 bot 1,012turn スパイク再発防止)。"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))

import clone_improve_lib as lib  # noqa: E402


def setup_function(_):
    lib._EVAL_TURNS["n"] = 0  # プロセス内カウンタを各テストでリセット


def test_under_limit_passes(monkeypatch):
    monkeypatch.setenv("EVAL_MAX_BOT_TURNS", "5")
    for _ in range(5):
        lib.eval_turn_guard()  # 5回までは素通り


def test_over_limit_raises(monkeypatch):
    monkeypatch.setenv("EVAL_MAX_BOT_TURNS", "3")
    for _ in range(3):
        lib.eval_turn_guard()
    with pytest.raises(RuntimeError, match="EVAL_MAX_BOT_TURNS"):
        lib.eval_turn_guard()  # 4回目で停止


def test_zero_disables(monkeypatch):
    monkeypatch.setenv("EVAL_MAX_BOT_TURNS", "0")
    for _ in range(1000):
        lib.eval_turn_guard()  # 0 = 無効 (大規模 run の明示 opt-out)


def test_default_is_300(monkeypatch):
    monkeypatch.delenv("EVAL_MAX_BOT_TURNS", raising=False)
    for _ in range(300):
        lib.eval_turn_guard()
    with pytest.raises(RuntimeError):
        lib.eval_turn_guard()
