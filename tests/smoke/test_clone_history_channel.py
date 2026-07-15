"""smoke test: clone_history.py の channel_id 拡張 (★2026-05-24 Tier 0)

既存 DM path の後方互換 + 新規 group path (channel_id) の動作 + scope filter。
"""
from __future__ import annotations

import importlib
import json
from pathlib import Path

import pytest


@pytest.mark.smoke
def test_append_dm_backward_compat(brain_root):
    """既存 path (channel_id 引数省略) で append + load → channel_id=None record。"""
    import clone_history
    importlib.reload(clone_history)

    clone_history.append(
        user_id="user_dm_001", role="user", text="DM message",
        user_display="Alice",
    )
    # raw record 確認
    path = clone_history._user_file("user_dm_001")
    line = path.read_text(encoding="utf-8").strip()
    rec = json.loads(line)
    assert rec["channel_id"] is None  # 後方互換: 省略時は None
    assert rec["text"] == "DM message"


@pytest.mark.smoke
def test_append_group_with_channel_id(brain_root):
    """channel_id 付き append → record に channel_id 保存。"""
    import clone_history
    importlib.reload(clone_history)

    clone_history.append(
        user_id="user_g_001", role="user", text="group message",
        user_display="Bob", channel_id="ch_abc",
    )
    path = clone_history._user_file("user_g_001")
    line = path.read_text(encoding="utf-8").strip()
    rec = json.loads(line)
    assert rec["channel_id"] == "ch_abc"
    assert rec["text"] == "group message"


@pytest.mark.smoke
def test_load_recent_scope_any_default(brain_root):
    """scope='any' (default) は channel_id 関係なく全件 (= 後方互換)。"""
    import clone_history
    importlib.reload(clone_history)

    clone_history.append("u1", "user", "dm1")
    clone_history.append("u1", "user", "group_a_1", channel_id="ch_a")
    clone_history.append("u1", "user", "group_b_1", channel_id="ch_b")

    recs = clone_history.load_recent("u1", n=10)
    assert len(recs) == 3
    texts = [r["content"] for r in recs]
    assert "dm1" in texts
    assert "group_a_1" in texts
    assert "group_b_1" in texts


@pytest.mark.smoke
def test_load_recent_scope_dm_only(brain_root):
    """scope='dm' で channel_id=None record のみ返る。"""
    import clone_history
    importlib.reload(clone_history)

    clone_history.append("u2", "user", "dm_a")
    clone_history.append("u2", "user", "group_msg", channel_id="ch_x")
    clone_history.append("u2", "user", "dm_b")

    recs = clone_history.load_recent("u2", n=10, scope="dm")
    texts = [r["content"] for r in recs]
    assert "dm_a" in texts
    assert "dm_b" in texts
    assert "group_msg" not in texts


@pytest.mark.smoke
def test_load_recent_scope_channel_filters(brain_root):
    """scope='channel' + channel_id で該当 group のみ返る。"""
    import clone_history
    importlib.reload(clone_history)

    clone_history.append("u3", "user", "dm_x")
    clone_history.append("u3", "user", "g_a_1", channel_id="ch_a")
    clone_history.append("u3", "user", "g_b_1", channel_id="ch_b")
    clone_history.append("u3", "user", "g_a_2", channel_id="ch_a")

    recs_a = clone_history.load_recent("u3", n=10, channel_id="ch_a", scope="channel")
    texts_a = [r["content"] for r in recs_a]
    assert texts_a == ["g_a_1", "g_a_2"]

    recs_b = clone_history.load_recent("u3", n=10, channel_id="ch_b", scope="channel")
    texts_b = [r["content"] for r in recs_b]
    assert texts_b == ["g_b_1"]


@pytest.mark.smoke
def test_load_recent_scope_channel_requires_channel_id(brain_root):
    """scope='channel' で channel_id 引数無いと ValueError。"""
    import clone_history
    importlib.reload(clone_history)

    with pytest.raises(ValueError, match="channel_id"):
        clone_history.load_recent("u4", scope="channel")


@pytest.mark.smoke
def test_legacy_records_treated_as_dm(brain_root):
    """既存 record (= channel_id field 無し) は scope='dm' で hit、'channel' で miss。"""
    import clone_history
    importlib.reload(clone_history)

    # 旧 schema record を直接書く (channel_id field 無し)
    path = clone_history._user_file("u_legacy")
    path.parent.mkdir(parents=True, exist_ok=True)
    legacy_rec = {
        "timestamp": "2026-05-01T10:00:00+09:00",
        "user_id": "u_legacy",
        "user_display": "Legacy User",
        "role": "user",
        "text": "old message",
    }
    path.write_text(json.dumps(legacy_rec, ensure_ascii=False) + "\n", encoding="utf-8")

    # scope='dm' で hit (= 後方互換、channel_id 無し = None として扱う)
    recs_dm = clone_history.load_recent("u_legacy", scope="dm")
    assert len(recs_dm) == 1
    assert recs_dm[0]["content"] == "old message"

    # scope='channel' で miss (= channel_id 無いので filter fail)
    recs_ch = clone_history.load_recent("u_legacy", channel_id="ch_x", scope="channel")
    assert recs_ch == []


@pytest.mark.smoke
def test_dump_user_channel_marker(brain_root):
    """dump_user で group 内発言は [G:xxxxxxxx] marker 表示。"""
    import clone_history
    importlib.reload(clone_history)

    clone_history.append("u_d", "user", "dm message", user_display="Test")
    clone_history.append("u_d", "user", "group message", channel_id="ch_abcdef01")

    dump = clone_history.dump_user("u_d", n=10)
    assert "dm message" in dump
    assert "group message" in dump
    assert "[G:ch_abcde]" in dump  # marker は先頭 8 文字
