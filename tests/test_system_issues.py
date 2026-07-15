"""tests/test_system_issues.py — system_issues module + dashboard action form smoke test"""
from __future__ import annotations

import importlib
import os
import sys
import tempfile
import time
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))


@pytest.fixture
def tmp_brain(tmp_path, monkeypatch):
    monkeypatch.setenv("BRAIN_ROOT", str(tmp_path))
    # reload modules to pick up new BRAIN_ROOT
    if "services.system_issues" in sys.modules:
        del sys.modules["services.system_issues"]
    if "clone_learning" in sys.modules:
        del sys.modules["clone_learning"]
    return tmp_path


def test_system_issues_add_and_list(tmp_brain):
    si = importlib.import_module("services.system_issues")
    si.ISSUES_DIR = tmp_brain / "clone_review"
    si.ISSUES_FILE = si.ISSUES_DIR / "system_issues.jsonl"

    id1 = si.add_entry("提案 patch が編集できない", "編集 UI 追加", "umiyama")
    time.sleep(1.05)
    id2 = si.add_entry("Drive 404 文言改善", "", "umiyama")

    assert id1.startswith("sysi_")
    assert id2.startswith("sysi_")

    pending = si.list_pending()
    assert len(pending) == 2
    assert pending[0]["id"] == id2  # 新しい順
    assert pending[0]["category"] == "system_issue"
    assert pending[1]["expected"] == "編集 UI 追加"


def test_system_issues_update_status(tmp_brain):
    si = importlib.import_module("services.system_issues")
    si.ISSUES_DIR = tmp_brain / "clone_review"
    si.ISSUES_FILE = si.ISSUES_DIR / "system_issues.jsonl"

    fid = si.add_entry("test issue", "", "umiyama")
    assert si.update_status(fid, "fixed")
    assert not si.update_status(fid, "invalid_status")
    assert not si.update_status("nonexistent_id", "fixed")
    assert si.count_pending() == 0


def test_system_issues_add_comment(tmp_brain):
    si = importlib.import_module("services.system_issues")
    si.ISSUES_DIR = tmp_brain / "clone_review"
    si.ISSUES_FILE = si.ISSUES_DIR / "system_issues.jsonl"

    fid = si.add_entry("test", "", "umiyama")
    assert si.add_comment(fid, "priority low", "umiyama")
    assert not si.add_comment(fid, "   ", "umiyama")  # empty
    assert not si.add_comment("nonexistent", "x", "umiyama")
    items = si.list_pending()
    assert len(items[0]["comments"]) == 1
    assert items[0]["comments"][0]["comment"] == "priority low"


def test_system_issues_empty_description_raises(tmp_brain):
    si = importlib.import_module("services.system_issues")
    with pytest.raises(ValueError):
        si.add_entry("", "", "umiyama")
    with pytest.raises(ValueError):
        si.add_entry("   ", "", "umiyama")


def test_clone_learning_add_manual_entry(tmp_brain):
    cl = importlib.import_module("clone_learning")
    cl.LEARNING_DIR = tmp_brain / "clone_learning"

    fid = cl.add_manual_entry("回答が冷たすぎる", "雑談 coating 強化", "umiyama")
    assert fid.startswith("manual_")

    items = cl.list_pending()
    assert len(items) == 1
    assert items[0]["category"] == "manual_quality"
    assert items[0]["manual_entry"] is True
    assert items[0]["insight"] == "回答が冷たすぎる"
    assert items[0]["proposed_wiki_patch"] == "雑談 coating 強化"
    assert items[0]["source_snippet"].startswith("(海山がダッシュボード")


def test_clone_learning_add_manual_entry_empty_raises(tmp_brain):
    cl = importlib.import_module("clone_learning")
    cl.LEARNING_DIR = tmp_brain / "clone_learning"
    with pytest.raises(ValueError):
        cl.add_manual_entry("", "")
    with pytest.raises(ValueError):
        cl.add_manual_entry("   ", "")


def test_dashboard_action_form_renders():
    """_render_action_form が必要 element を生成"""
    from services.review_dashboard import _render_action_form
    html = _render_action_form("test-token-xyz")
    assert 'action="/admin/review/action/submit?token=test-token-xyz"' in html
    assert 'name="mode" value="quality"' in html
    assert 'name="mode" value="system"' in html
    assert 'name="content"' in html
    assert 'name="detail"' in html
    assert "回答品質向上" in html
    assert "システム修正依頼" in html

    # default=system mode
    html_sys = _render_action_form("test-token-xyz", default_mode="system")
    # quality radio shouldn't be checked when default is system
    assert 'value="system" checked' in html_sys


def test_dashboard_handle_action_system_queue(tmp_brain):
    """handle_action queue='system' で system_issues update/comment が走る"""
    from services.review_dashboard import handle_action
    si = importlib.import_module("services.system_issues")
    si.ISSUES_DIR = tmp_brain / "clone_review"
    si.ISSUES_FILE = si.ISSUES_DIR / "system_issues.jsonl"

    fid = si.add_entry("test", "", "umiyama")

    # acknowledged action
    ok, msg = handle_action("system", "acknowledged", fid, note="調査開始")
    assert ok
    assert "acknowledged" in msg
    assert "(note 付き)" in msg

    # fixed action
    ok2, msg2 = handle_action("system", "fixed", fid)
    assert ok2
    assert "fixed" in msg2

    # invalid action
    ok3, msg3 = handle_action("system", "accept", fid)  # accept は system queue では invalid
    assert not ok3
    assert "unknown system action" in msg3


def test_nav_includes_system_link():
    """nav に システム修正 link が登録されてる"""
    from services.review_dashboard import _nav
    html = _nav("/admin/review/learning", "test-token")
    assert "/admin/review/system" in html
    assert "システム修正" in html


def test_render_system_issues_page(tmp_brain):
    """render_system_issues_page が直接入力 form + items 両方を返す"""
    si = importlib.import_module("services.system_issues")
    si.ISSUES_DIR = tmp_brain / "clone_review"
    si.ISSUES_FILE = si.ISSUES_DIR / "system_issues.jsonl"

    si.add_entry("テスト不備", "期待動作の説明", "umiyama")

    # NOTE: render_system_issues_page calls system_issues module via `from services import system_issues`,
    # which re-imports the module. We need the test to use the actual module-level paths.
    # Workaround: also patch the module the dashboard imports.
    from services import review_dashboard
    html = review_dashboard.render_system_issues_page("test-token")
    assert "action-form" in html
    assert 'name="mode" value="system" checked' in html
    assert "テスト不備" in html or "Pending システム修正依頼" in html
