"""smoke test: clone_sleep_time_agent の memory 破壊防止 guard (★2026-06-07 エージェント評価)

sleep_time は memory を全文上書きするので、LLM が誤って大幅短縮した updated を無条件採用すると
記憶が大量喪失する。激減 guard + 上書き前 backup + 退避 draft の回帰保護。destructive なので必須。
"""
from __future__ import annotations

import importlib
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent.parent


@pytest.fixture
def mod(monkeypatch, tmp_path):
    monkeypatch.setenv("BRAIN_ROOT", str(tmp_path))
    sys.path.insert(0, str(REPO / "scripts"))
    import clone_sleep_time_agent as s
    importlib.reload(s)
    return s


@pytest.mark.smoke
def test_drastic_shrink_rejected(mod):
    """既存の 50% 未満へ激減した updated は採用しない (記憶喪失防止)。"""
    accept, reason = mod._should_accept_update("x" * 2000, "y" * 200)
    assert accept is False and reason.startswith("shrink_guard")


@pytest.mark.smoke
def test_modest_change_accepted(mod):
    """60% への微減は正常な整理として採用。"""
    accept, _ = mod._should_accept_update("x" * 2000, "y" * 1200)
    assert accept is True


@pytest.mark.smoke
def test_small_memory_not_guarded(mod):
    """500字未満の memory は guard 対象外 (小 memory の正常整理を妨げない)。"""
    accept, _ = mod._should_accept_update("x" * 300, "y" * 50)
    assert accept is True


@pytest.mark.smoke
def test_empty_and_nochange_rejected(mod):
    big = "x" * 1000
    assert mod._should_accept_update(big, "   ")[0] is False
    assert mod._should_accept_update(big, big)[0] is False


@pytest.mark.smoke
def test_backup_written_before_overwrite(mod, tmp_path):
    """上書き前 backup が書かれる (last-good 復元可能)。"""
    mod._backup_memory("user-abc", "古い memory 内容")
    bk = tmp_path / "clone_memory_backup" / "user-abc.md"
    assert bk.exists() and bk.read_text(encoding="utf-8") == "古い memory 内容"


@pytest.mark.smoke
def test_shrink_draft_preserves_both(mod, tmp_path):
    """激減 reject 時、既存と LLM 提案の両方を draft に退避 (海山が確認可能)。"""
    mod._save_shrink_draft("user-xyz", "既存" * 800, "提案" * 30)
    drafts = list((tmp_path / "clone_improve" / "sleep_time_drafts").glob("*-SHRINK.md"))
    assert len(drafts) == 1
    txt = drafts[0].read_text(encoding="utf-8")
    assert "既存 memory" in txt and "未採用" in txt
