"""tests/test_escalate_to_system.py — learning item → system_issue 格上げ test

★2026-05-25 海山指示: pending learning item を per-item で system_issue に reclassify。
"""
from __future__ import annotations

import importlib
import json
import sys
import time
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))


@pytest.fixture
def tmp_brain(tmp_path, monkeypatch):
    monkeypatch.setenv("BRAIN_ROOT", str(tmp_path))
    for mod_name in ("services.system_issues", "clone_learning"):
        if mod_name in sys.modules:
            del sys.modules[mod_name]
    return tmp_path


def test_get_entry_by_id(tmp_brain):
    cl = importlib.import_module("clone_learning")
    cl.LEARNING_DIR = tmp_brain / "clone_learning"
    cl.LEARNING_DIR.mkdir(parents=True, exist_ok=True)

    fid = cl.add_manual_entry("テスト insight", "テスト patch", "umiyama")
    entry = cl.get_entry_by_id(fid)
    assert entry is not None
    assert entry["id"] == fid
    assert entry["insight"] == "テスト insight"
    assert entry["proposed_wiki_patch"] == "テスト patch"

    assert cl.get_entry_by_id("nonexistent_id") is None


def test_escalate_to_system_basic(tmp_brain):
    cl = importlib.import_module("clone_learning")
    si = importlib.import_module("services.system_issues")
    cl.LEARNING_DIR = tmp_brain / "clone_learning"
    cl.LEARNING_DIR.mkdir(parents=True, exist_ok=True)
    si.ISSUES_DIR = tmp_brain / "clone_review"
    si.ISSUES_FILE = si.ISSUES_DIR / "system_issues.jsonl"

    fid = cl.add_manual_entry(
        "[too_passive] bot 応答が短すぎる",
        "もっと詳しく説明する wiki patch",
        "umiyama",
    )
    sysi_id = cl.escalate_to_system(fid, note="bot crash 疑い、調査必要", reviewer="umiyama")

    assert sysi_id is not None
    assert sysi_id.startswith("sysi_")

    # system_issues に新 entry 作成
    items = si.list_pending()
    assert len(items) == 1
    new_item = items[0]
    assert new_item["id"] == sysi_id
    assert new_item["category"] == "system_issue"
    assert "[too_passive] bot 応答が短すぎる" in new_item["description"]
    assert "bot crash 疑い" in new_item["expected"]
    assert "もっと詳しく説明する wiki patch" in new_item["expected"]

    # 元 learning item は rejected
    entry = cl.get_entry_by_id(fid)
    assert entry["status"] == "rejected"
    # comment に reference が入ってるはず (= clone_learning は text key 採用)
    comments = entry.get("comments", [])
    assert any(sysi_id in (c.get("text", "") or c.get("comment", "")) for c in comments), \
        f"reference note not in comments: {comments}"


def test_escalate_to_system_missing_id(tmp_brain):
    cl = importlib.import_module("clone_learning")
    cl.LEARNING_DIR = tmp_brain / "clone_learning"
    cl.LEARNING_DIR.mkdir(parents=True, exist_ok=True)
    assert cl.escalate_to_system("nonexistent_id") is None


def test_escalate_to_system_with_snippet(tmp_brain):
    cl = importlib.import_module("clone_learning")
    si = importlib.import_module("services.system_issues")
    cl.LEARNING_DIR = tmp_brain / "clone_learning"
    cl.LEARNING_DIR.mkdir(parents=True, exist_ok=True)
    si.ISSUES_DIR = tmp_brain / "clone_review"
    si.ISSUES_FILE = si.ISSUES_DIR / "system_issues.jsonl"

    # LLM auto-discovered item (= snippet 付き)
    from datetime import datetime
    rec = {
        "id": "auto_test001",
        "timestamp": datetime.now().astimezone().isoformat(timespec="seconds"),
        "category": "response_quality",
        "insight": "[too_passive] bot 応答が error fallback",
        "source_snippet": "USER: 売上は?\nBOT: 申し訳ありません。少し時間を置いて再度お試しください。",
        "proposed_wiki_patch": "より詳しく説明する patch",
        "status": "pending",
    }
    fname = cl.LEARNING_DIR / "2026-05.jsonl"
    fname.write_text(json.dumps(rec, ensure_ascii=False) + "\n", encoding="utf-8")

    sysi_id = cl.escalate_to_system("auto_test001", note="retrieval timeout 疑い")
    assert sysi_id is not None

    items = si.list_pending()
    new_item = items[0]
    assert "USER: 売上は?" in new_item["description"]
    assert "[BOT 応答]" not in new_item["description"]  # 違う section header
    assert "[抽出元 会話]" in new_item["description"]
    assert "retrieval timeout 疑い" in new_item["expected"]


def test_handle_action_escalate_learning(tmp_brain):
    """handle_action queue='learning' action='escalate' で reclassify"""
    cl = importlib.import_module("clone_learning")
    si = importlib.import_module("services.system_issues")
    cl.LEARNING_DIR = tmp_brain / "clone_learning"
    cl.LEARNING_DIR.mkdir(parents=True, exist_ok=True)
    si.ISSUES_DIR = tmp_brain / "clone_review"
    si.ISSUES_FILE = si.ISSUES_DIR / "system_issues.jsonl"

    fid = cl.add_manual_entry("test escalate via handle_action", "patch", "umiyama")

    from services.review_dashboard import handle_action
    ok, msg = handle_action("learning", "escalate", fid, note="システム不備の方")
    assert ok
    assert "system_issue" in msg
    assert "格上げ" in msg

    # system_issues に新 entry
    items = si.list_pending()
    assert len(items) == 1
    assert "システム不備の方" in items[0]["expected"]


def test_handle_action_escalate_invalid_id(tmp_brain):
    cl = importlib.import_module("clone_learning")
    si = importlib.import_module("services.system_issues")
    cl.LEARNING_DIR = tmp_brain / "clone_learning"
    cl.LEARNING_DIR.mkdir(parents=True, exist_ok=True)
    si.ISSUES_DIR = tmp_brain / "clone_review"
    si.ISSUES_FILE = si.ISSUES_DIR / "system_issues.jsonl"
    from services.review_dashboard import handle_action
    ok, msg = handle_action("learning", "escalate", "nonexistent_id")
    assert not ok
    assert "失敗" in msg
