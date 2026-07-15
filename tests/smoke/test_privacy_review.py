"""smoke test: clone_memory_privacy_review (項目 10)。"""
from __future__ import annotations

import json
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(REPO_ROOT / "scripts"))


@pytest.mark.smoke
def test_module_imports():
    """script が import できる + 必要関数が存在。"""
    import clone_memory_privacy_review as mod
    assert hasattr(mod, "review_one_user")
    assert hasattr(mod, "select_users_to_review")
    assert hasattr(mod, "PRIVACY_REVIEW_PROMPT")


@pytest.mark.smoke
def test_prompt_contains_6_axes():
    """PRIVACY_REVIEW_PROMPT が 6 観点全部を含む。"""
    import clone_memory_privacy_review as mod
    prompt = mod.PRIVACY_REVIEW_PROMPT
    for axis in ("個人特定情報", "健康深刻", "家族プライベート",
                 "第三者の評価", "M&A", "性的"):
        assert axis in prompt, f"6 観点に {axis} が無い"


@pytest.mark.smoke
def test_select_users_empty(tmp_path, monkeypatch):
    """memory dir 空なら select は空 list を返す。"""
    import clone_memory_privacy_review as mod
    monkeypatch.setattr(mod, "MEMORY_DIR", tmp_path / "empty")
    assert mod.select_users_to_review(10) == []


@pytest.mark.smoke
def test_select_users_returns_user_ids(tmp_path, monkeypatch):
    """memory ファイルがあれば user_id を返す。"""
    import clone_memory_privacy_review as mod
    mem_dir = tmp_path / "memory"
    mem_dir.mkdir()
    (mem_dir / "u1.md").write_text("test", encoding="utf-8")
    (mem_dir / "u2.md").write_text("test", encoding="utf-8")
    monkeypatch.setattr(mod, "MEMORY_DIR", mem_dir)
    monkeypatch.setattr(mod, "LOG_PATH", tmp_path / "nonexistent.jsonl")

    users = mod.select_users_to_review(10)
    assert set(users) == {"u1", "u2"}


@pytest.mark.smoke
def test_select_users_prioritizes_unreviewed(tmp_path, monkeypatch):
    """過去 review されてない user が優先される。"""
    import clone_memory_privacy_review as mod
    mem_dir = tmp_path / "memory"
    mem_dir.mkdir()
    (mem_dir / "u_old.md").write_text("x", encoding="utf-8")
    (mem_dir / "u_new.md").write_text("x", encoding="utf-8")
    monkeypatch.setattr(mod, "MEMORY_DIR", mem_dir)

    # u_old は最近 review された記録あり、u_new は未 review
    log_path = tmp_path / "log.jsonl"
    log_path.write_text(json.dumps({
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "reviewed_users": ["u_old"],
    }) + "\n", encoding="utf-8")
    monkeypatch.setattr(mod, "LOG_PATH", log_path)

    users = mod.select_users_to_review(10)
    # u_new (未 review = last_reviewed 0) が先に来る
    assert users[0] == "u_new"


@pytest.mark.smoke
async def test_review_one_user_no_memory(tmp_path, monkeypatch):
    """memory ファイルが無い user は skipped 扱い。"""
    import clone_memory_privacy_review as mod
    monkeypatch.setattr(mod, "MEMORY_DIR", tmp_path)

    r = await mod.review_one_user("nonexistent_user", dry_run=True)
    assert r.get("skipped") is True
    assert r.get("reason") == "no_memory"


@pytest.mark.smoke
async def test_review_one_user_too_short(tmp_path, monkeypatch):
    """memory が極端に短い (200 字未満) なら skipped。"""
    import clone_memory_privacy_review as mod
    monkeypatch.setattr(mod, "MEMORY_DIR", tmp_path)
    (tmp_path / "u_short.md").write_text("# short", encoding="utf-8")

    r = await mod.review_one_user("u_short", dry_run=True)
    assert r.get("skipped") is True
    assert r.get("reason") == "too_short"


@pytest.mark.smoke
def test_archive_dir_path():
    """ARCHIVE_DIR が REVIEW_DIR 配下にある。"""
    import clone_memory_privacy_review as mod
    assert mod.ARCHIVE_DIR.parent == mod.REVIEW_DIR
    assert mod.ARCHIVE_DIR.name == "archived"


@pytest.mark.smoke
def test_daily_batch_size_default():
    """DAILY_BATCH_SIZE が妥当 (5-50)。"""
    import clone_memory_privacy_review as mod
    assert 5 <= mod.DAILY_BATCH_SIZE <= 50
