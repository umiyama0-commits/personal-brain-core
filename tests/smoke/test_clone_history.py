"""smoke test: clone_history の JSONL 読み書き。"""
from __future__ import annotations

import importlib
import json
from pathlib import Path

import pytest


@pytest.mark.smoke
def test_append_and_load_recent(brain_root, monkeypatch):
    """append → load_recent で書いたものが読めること。"""
    # BRAIN_ROOT を env から取らせるため reload
    import clone_history
    importlib.reload(clone_history)

    clone_history.append(
        user_id="user_test_001",
        role="user",
        text="テストメッセージ",
        user_display="テスト 太郎",
    )
    # load_recent は LLM messages 用に {role, content} 形式で返す (text → content にリネーム)
    recs = clone_history.load_recent("user_test_001", n=5)
    assert len(recs) == 1
    assert recs[0]["content"] == "テストメッセージ"
    assert recs[0]["role"] == "user"


@pytest.mark.smoke
def test_load_recent_with_existing(sample_clone_history):
    """fixture で投入した record が読めること。"""
    import clone_history
    importlib.reload(clone_history)

    alice_recs = clone_history.load_recent("user_alice", n=10)
    assert len(alice_recs) == 3
    # 時系列順、load_recent は content key で返す
    assert alice_recs[0]["content"] == "店舗売上どうですか?"
    assert alice_recs[-1]["content"] == "ありがとう"


@pytest.mark.smoke
def test_list_users(sample_clone_history):
    """list_users で全 user が返ること。"""
    import clone_history
    importlib.reload(clone_history)

    users = clone_history.list_users()
    user_ids = {u["user_id"] for u in users}
    assert "user_alice" in user_ids
    assert "user_bob" in user_ids


@pytest.mark.smoke
def test_forget(sample_clone_history):
    """forget で user の history が消えること。"""
    import clone_history
    importlib.reload(clone_history)

    assert clone_history.forget("user_alice") is True
    assert clone_history.forget("user_alice") is False  # 2 度目は False
    recs = clone_history.load_recent("user_alice", n=5)
    assert len(recs) == 0


@pytest.mark.smoke
def test_user_file_path_safe(brain_root):
    """user_id にスラッシュが含まれてもパスが壊れないこと。"""
    import clone_history
    importlib.reload(clone_history)

    # / が含まれる怪しい user_id
    p = clone_history._user_file("evil/../../etc/passwd")
    # 必ず HISTORY_DIR 配下のはず
    assert clone_history.HISTORY_DIR in p.parents or p.parent == clone_history.HISTORY_DIR
