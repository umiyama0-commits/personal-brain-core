"""tests/test_voice_align_dashboard.py — Vapi voice-align 蒸留状況 dashboard test

★2026-05-26 海山指示「vapi との会話もダッシュボードで蒸留状況のチェック」
- 8 次元 coverage 可視化
- pending 蒸留案 list + 詳細 per-item accept/reject
"""
from __future__ import annotations

import importlib
import json
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))


@pytest.fixture
def tmp_brain(tmp_path, monkeypatch):
    monkeypatch.setenv("BRAIN_ROOT", str(tmp_path))
    for mod_name in ("alignment_interview",):
        if mod_name in sys.modules:
            del sys.modules[mod_name]
    return tmp_path


def test_render_voice_align_no_data(tmp_brain):
    """data 無くても crash しない (= empty state 表示)"""
    from services.review_dashboard import render_voice_align_page
    html = render_voice_align_page("test-token")
    # 必要 elements
    assert "音声 align" in html
    assert "8 次元 カバレッジ" in html or "coverage" in html.lower() or "alignment_interview 読込失敗" in html


def test_render_voice_align_with_pending(tmp_brain):
    import alignment_interview as ai
    align_dir = tmp_brain / "alignment"
    align_dir.mkdir(parents=True, exist_ok=True)
    extracted_dir = align_dir / "interview_extracted"
    extracted_dir.mkdir(parents=True, exist_ok=True)
    ai.ALIGN_DIR = align_dir
    ai.COVERAGE_FILE = align_dir / "interview_coverage.json"
    ai.EXTRACTED_DIR = extracted_dir

    # Pending extraction 1 件 seed
    rec = {
        "extracted_at": "2026-05-26T10:00:00+09:00",
        "session_summary": "車内で過去の起業エピソード雑談",
        "items": [
            {"category": "biography", "insight": "26 歳で借金を抱えた", "evidence_quote": "ホテルの一室で...", "confidence": "high"},
            {"category": "value_root", "insight": "失敗を恐れない", "evidence_quote": "あれが原点", "confidence": "medium"},
        ],
        "status": "pending_review",
    }
    f = extracted_dir / "2026-05-26-1000-test.json"
    f.write_text(json.dumps(rec, ensure_ascii=False), encoding="utf-8")

    from services.review_dashboard import render_voice_align_page
    html = render_voice_align_page("test-token")
    assert "車内で過去の起業エピソード雑談" in html
    assert "2 items" in html or "items" in html
    # action form
    assert 'name="action" value="accept_all"' in html
    assert "📄 詳細" in html or "detail" in html.lower()


def test_render_voice_align_detail(tmp_brain):
    import alignment_interview as ai
    align_dir = tmp_brain / "alignment"
    align_dir.mkdir(parents=True, exist_ok=True)
    extracted_dir = align_dir / "interview_extracted"
    extracted_dir.mkdir(parents=True, exist_ok=True)
    ai.ALIGN_DIR = align_dir
    ai.COVERAGE_FILE = align_dir / "interview_coverage.json"
    ai.EXTRACTED_DIR = extracted_dir

    rec = {
        "extracted_at": "2026-05-26T10:00:00+09:00",
        "session_summary": "test session",
        "items": [
            {"category": "biography", "insight": "item A", "evidence_quote": "evidence A", "confidence": "high"},
            {"category": "value_root", "insight": "item B", "evidence_quote": "", "confidence": "low"},
        ],
        "status": "pending_review",
    }
    f = extracted_dir / "test-detail.json"
    f.write_text(json.dumps(rec, ensure_ascii=False), encoding="utf-8")

    from services.review_dashboard import render_voice_align_detail_page
    html = render_voice_align_detail_page("test-token", "test-detail.json")
    assert "item A" in html
    assert "item B" in html
    assert "evidence A" in html
    # checkbox indices
    assert 'name="indices" value="0"' in html
    assert 'name="indices" value="1"' in html
    # selective accept button
    assert 'name="action" value="accept_selected"' in html
    assert 'name="action" value="accept_all"' in html


def test_render_voice_align_detail_missing_file(tmp_brain):
    from services.review_dashboard import render_voice_align_detail_page
    html = render_voice_align_detail_page("test-token", "nonexistent.json")
    assert "見つからず" in html or "not found" in html.lower() or "ファイル無し" in html


def test_handle_voice_align_action_reject(tmp_brain):
    import alignment_interview as ai
    align_dir = tmp_brain / "alignment"
    align_dir.mkdir(parents=True, exist_ok=True)
    extracted_dir = align_dir / "interview_extracted"
    extracted_dir.mkdir(parents=True, exist_ok=True)
    ai.ALIGN_DIR = align_dir
    ai.COVERAGE_FILE = align_dir / "interview_coverage.json"
    ai.EXTRACTED_DIR = extracted_dir

    rec = {"items": [{"category": "biography", "insight": "x"}], "status": "pending_review"}
    f = extracted_dir / "rej.json"
    f.write_text(json.dumps(rec), encoding="utf-8")

    from services.review_dashboard import handle_voice_align_action
    ok, msg = handle_voice_align_action("rej.json", "reject")
    assert ok
    assert "rejected" in msg.lower()

    # status 更新確認
    loaded = json.loads(f.read_text())
    assert loaded["status"] == "rejected"


def test_handle_voice_align_action_accept_all(tmp_brain):
    import alignment_interview as ai
    align_dir = tmp_brain / "alignment"
    align_dir.mkdir(parents=True, exist_ok=True)
    extracted_dir = align_dir / "interview_extracted"
    extracted_dir.mkdir(parents=True, exist_ok=True)
    wiki_dir = tmp_brain / "wiki"
    wiki_dir.mkdir(parents=True, exist_ok=True)
    ai.ALIGN_DIR = align_dir
    ai.COVERAGE_FILE = align_dir / "interview_coverage.json"
    ai.EXTRACTED_DIR = extracted_dir
    ai.WIKI_DIR = wiki_dir

    rec = {
        "extracted_at": "2026-05-26T10:00:00+09:00",
        "items": [
            {"category": "biography", "insight": "test insight", "evidence_quote": "ev", "confidence": "high"},
            {"category": "value_root", "insight": "another", "evidence_quote": "", "confidence": "medium"},
        ],
        "status": "pending_review",
    }
    f = extracted_dir / "acc.json"
    f.write_text(json.dumps(rec, ensure_ascii=False), encoding="utf-8")

    from services.review_dashboard import handle_voice_align_action
    ok, msg = handle_voice_align_action("acc.json", "accept_all")
    assert ok, msg
    assert "applied" in msg.lower()
    assert "2" in msg

    # wiki に書かれた
    bio = wiki_dir / "interview" / "biography.md"
    assert bio.exists()
    assert "test insight" in bio.read_text()


def test_handle_voice_align_action_accept_selected(tmp_brain):
    import alignment_interview as ai
    align_dir = tmp_brain / "alignment"
    align_dir.mkdir(parents=True, exist_ok=True)
    extracted_dir = align_dir / "interview_extracted"
    extracted_dir.mkdir(parents=True, exist_ok=True)
    wiki_dir = tmp_brain / "wiki"
    wiki_dir.mkdir(parents=True, exist_ok=True)
    ai.ALIGN_DIR = align_dir
    ai.COVERAGE_FILE = align_dir / "interview_coverage.json"
    ai.EXTRACTED_DIR = extracted_dir
    ai.WIKI_DIR = wiki_dir

    rec = {
        "extracted_at": "2026-05-26T10:00:00+09:00",
        "items": [
            {"category": "biography", "insight": "item0", "evidence_quote": "", "confidence": "high"},
            {"category": "value_root", "insight": "item1", "evidence_quote": "", "confidence": "medium"},
            {"category": "judgment", "insight": "item2", "evidence_quote": "", "confidence": "high"},
        ],
        "status": "pending_review",
    }
    f = extracted_dir / "sel.json"
    f.write_text(json.dumps(rec, ensure_ascii=False), encoding="utf-8")

    from services.review_dashboard import handle_voice_align_action
    # 0 と 2 だけ採用 (= 1 はスキップ)
    ok, msg = handle_voice_align_action("sel.json", "accept_selected", accepted_indices=[0, 2])
    assert ok, msg
    assert "applied" in msg.lower()
    assert "2" in msg  # 2 件 applied

    # biography + judgment 書かれた、value-roots は書かれない
    bio = wiki_dir / "interview" / "biography.md"
    jud = wiki_dir / "interview" / "judgment.md"
    val = wiki_dir / "interview" / "value-roots.md"
    assert bio.exists() and "item0" in bio.read_text()
    assert jud.exists() and "item2" in jud.read_text()
    assert not val.exists() or "item1" not in val.read_text()


def test_handle_voice_align_action_unknown(tmp_brain):
    from services.review_dashboard import handle_voice_align_action
    ok, msg = handle_voice_align_action("any.json", "destroy")
    assert not ok
    assert "unknown" in msg


def test_handle_voice_align_action_no_filename(tmp_brain):
    from services.review_dashboard import handle_voice_align_action
    ok, msg = handle_voice_align_action("", "accept_all")
    assert not ok


def test_nav_includes_voice_align_link():
    from services.review_dashboard import _nav
    html = _nav("/admin/review/learning", "test-token")
    assert "/admin/review/voice-align" in html
    assert "音声align" in html
