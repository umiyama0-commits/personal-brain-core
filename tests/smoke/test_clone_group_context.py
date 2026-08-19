"""smoke test: clone_group_context.py (★2026-05-24 Tier 0 LINE WORKS group 対応)

per-group context memory の load / save / list / forget の sanity test。
clone_memory.py と同構造なので test pattern も類似。
"""
from __future__ import annotations

import importlib
import json
from pathlib import Path

import pytest


@pytest.mark.smoke
def test_module_imports():
    """clone_group_context module 読込可能 + 主要関数存在。"""
    import clone_group_context
    assert hasattr(clone_group_context, "load")
    assert hasattr(clone_group_context, "load_with_meta")
    assert hasattr(clone_group_context, "save")
    assert hasattr(clone_group_context, "list_channels")
    assert hasattr(clone_group_context, "dump_channel")
    assert hasattr(clone_group_context, "forget")
    assert hasattr(clone_group_context, "find_channels")
    assert hasattr(clone_group_context, "DEFAULT_BODY")
    # 4 section が default body に含まれる
    body = clone_group_context.DEFAULT_BODY
    assert "## Group Profile" in body
    assert "## Ongoing Topics" in body
    assert "## Recent Events" in body
    assert "## Group Culture" in body


@pytest.mark.smoke
def test_load_returns_default_when_no_file(brain_root):
    """file が無い channel に対して DEFAULT_BODY が返る。"""
    import clone_group_context
    importlib.reload(clone_group_context)
    body = clone_group_context.load("nonexistent_channel_id")
    assert body == clone_group_context.DEFAULT_BODY


@pytest.mark.smoke
def test_save_and_load_roundtrip(brain_root):
    """save → load で書いた内容が読めること、frontmatter が付くこと。"""
    import clone_group_context
    importlib.reload(clone_group_context)

    body = """## Group Profile
営業本部 review group、メンバー 12 名

## Ongoing Topics
- 龍仁出店 (2026-05-15〜)

## Recent Events
- 2026-05-23: 競合動向報告

## Group Culture
数字 first、結論先出し
"""
    clone_group_context.save(
        "ch_test_001", body,
        channel_display="営業本部チーム",
        turn_count=5, member_count=12,
    )
    # load_with_meta で frontmatter + body 両方確認
    fm, loaded_body = clone_group_context.load_with_meta("ch_test_001")
    assert fm["channel_id"] == "ch_test_001"
    assert fm["channel_display"] == "営業本部チーム"
    assert fm["turn_count"] == "5"
    assert fm["member_count"] == "12"
    assert "Group Profile" in loaded_body
    assert "龍仁出店" in loaded_body


@pytest.mark.smoke
def test_turn_count_increment_preserved(brain_root):
    """turn_count を省略すると既存値が引き継がれる。"""
    import clone_group_context
    importlib.reload(clone_group_context)

    body1 = "## Group Profile\n初回\n## Ongoing Topics\n\n## Recent Events\n\n## Group Culture\n"
    clone_group_context.save("ch_002", body1, turn_count=3)
    fm1, _ = clone_group_context.load_with_meta("ch_002")
    assert fm1["turn_count"] == "3"

    # turn_count 省略で save → 引き継がれる
    body2 = "## Group Profile\n更新\n## Ongoing Topics\n\n## Recent Events\n\n## Group Culture\n"
    clone_group_context.save("ch_002", body2)
    fm2, _ = clone_group_context.load_with_meta("ch_002")
    assert fm2["turn_count"] == "3"


@pytest.mark.smoke
def test_list_channels(brain_root):
    """list_channels で複数 channel が返る。"""
    import clone_group_context
    importlib.reload(clone_group_context)

    clone_group_context.save(
        "ch_a", "## Group Profile\nA\n## Ongoing Topics\n\n## Recent Events\n\n## Group Culture\n",
        channel_display="Group A", turn_count=10, member_count=5,
    )
    clone_group_context.save(
        "ch_b", "## Group Profile\nB\n## Ongoing Topics\n\n## Recent Events\n\n## Group Culture\n",
        channel_display="Group B", turn_count=3, member_count=8,
    )

    channels = clone_group_context.list_channels()
    ids = {c["channel_id"] for c in channels}
    assert "ch_a" in ids
    assert "ch_b" in ids
    # 各 record の必須 field
    for c in channels:
        assert "turn_count" in c
        assert "member_count" in c
        assert "display" in c


@pytest.mark.smoke
def test_forget_removes_file(brain_root):
    """forget で context file が消える、2 回目は False。"""
    import clone_group_context
    importlib.reload(clone_group_context)

    clone_group_context.save(
        "ch_forget", "## Group Profile\n\n## Ongoing Topics\n\n## Recent Events\n\n## Group Culture\n",
        turn_count=1,
    )
    assert clone_group_context.forget("ch_forget") is True
    assert clone_group_context.forget("ch_forget") is False
    # load は DEFAULT_BODY 返る
    assert clone_group_context.load("ch_forget") == clone_group_context.DEFAULT_BODY


@pytest.mark.smoke
def test_channel_id_path_safe(brain_root):
    """channel_id にスラッシュが含まれてもパスが壊れない。"""
    import clone_group_context
    importlib.reload(clone_group_context)

    # / が含まれる怪しい channel_id
    p = clone_group_context._channel_file("evil/../../etc/passwd")
    assert clone_group_context.GROUP_CONTEXT_DIR in p.parents or p.parent == clone_group_context.GROUP_CONTEXT_DIR


@pytest.mark.smoke
def test_find_channels_prefix(brain_root):
    """find_channels で prefix 一致する channel_id が返る。"""
    import clone_group_context
    importlib.reload(clone_group_context)

    body = "## Group Profile\n\n## Ongoing Topics\n\n## Recent Events\n\n## Group Culture\n"
    clone_group_context.save("abc_001", body, turn_count=1)
    clone_group_context.save("abc_002", body, turn_count=1)
    clone_group_context.save("xyz_001", body, turn_count=1)

    matches = clone_group_context.find_channels("abc")
    assert set(matches) == {"abc_001", "abc_002"}
    assert clone_group_context.find_channels("") == []
