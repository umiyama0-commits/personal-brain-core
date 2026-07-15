"""smoke test: macbook ↔ Mac Studio 同期 mechanism (★2026-05-27 海山指示 a+b)

(a) CLAUDE.md 1.16: macbook (= Claude) 着手前 git fetch 必須
(b) auto_deploy.sh deploy event LINE Push 通知 (= 両 PC commit を 海山が即把握)
"""
from __future__ import annotations

from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent.parent


# ─── (a) CLAUDE.md reflex memo ─────
@pytest.mark.smoke
def test_claude_md_has_macbook_fetch_reflex():
    """CLAUDE.md 1.16 に macbook 着手前 git fetch 規律."""
    src = (REPO / "CLAUDE.md").read_text(encoding="utf-8")
    assert "1.16" in src
    assert "git fetch origin main" in src
    # 重複実装 / merge conflict の警告
    assert "重複実装" in src or "merge conflict" in src
    # 実例 reference (= /talk vs /video-align)
    assert "/talk" in src or "/video-align" in src


# ─── (b) auto_deploy LINE Push 通知 ─────
@pytest.mark.smoke
def test_auto_deploy_notifies_on_deploy_ok():
    """auto_deploy.sh が deploy ok 時 LINE Push (= 「📥 deploy ok: ...」)."""
    src = (REPO / "scripts" / "auto_deploy.sh").read_text(encoding="utf-8")
    # deploy ok 通知 phrase
    assert "📥 deploy ok" in src
    # commit hash / subject / author を 埋め込み
    assert "DEPLOY_HASH" in src
    assert "DEPLOY_SUBJECT" in src
    assert "DEPLOY_AUTHOR" in src
    # _alert 関数経由 (= 既存)
    assert '_alert "📥' in src


@pytest.mark.smoke
def test_auto_deploy_notifies_on_no_rebuild_sync():
    """auto_deploy.sh が md/sh のみ変更でも sync 通知 (= 進捗同期目的)."""
    src = (REPO / "scripts" / "auto_deploy.sh").read_text(encoding="utf-8")
    assert "📥 sync ok" in src


@pytest.mark.smoke
def test_auto_deploy_still_alerts_on_build_failure():
    """既存 build 失敗 alert は維持 (= 同期通知追加で旧 alert 壊さない)."""
    src = (REPO / "scripts" / "auto_deploy.sh").read_text(encoding="utf-8")
    assert "🚨 auto_deploy: docker build 失敗" in src
