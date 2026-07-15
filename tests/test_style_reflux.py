"""tests/test_style_reflux.py — audit/feedback/learning を style 改善 proposal に集約

★2026-05-26 海山 B1+B3「audit fail / feedback を style へ逆流」: 直近 30 日の failure
pattern を分類 → 頻度 sort → 改善 proposal 生成 → 週次 LINE Push。
"""
from __future__ import annotations

import importlib
import json
import sys
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "scripts"))

JST = timezone(timedelta(hours=9))


@pytest.fixture
def tmp_brain(tmp_path, monkeypatch):
    monkeypatch.setenv("BRAIN_ROOT", str(tmp_path))
    if "style_reflux" in sys.modules:
        del sys.modules["style_reflux"]
    mod = importlib.import_module("style_reflux")
    mod.AUDIT_DIR = tmp_path / "clone_audit"
    mod.FEEDBACK_DIR = tmp_path / "clone_feedback"
    mod.LEARNING_DIR = tmp_path / "clone_learning"
    mod.OUT_DIR = tmp_path / "clone_improve" / "style_reflux"
    for d in (mod.AUDIT_DIR, mod.FEEDBACK_DIR, mod.LEARNING_DIR, mod.OUT_DIR):
        d.mkdir(parents=True, exist_ok=True)
    return mod


def _ts(d: date, hour: int = 10) -> str:
    return datetime(d.year, d.month, d.day, hour, 0, 0, tzinfo=JST).isoformat()


def test_classify_text_keywords(tmp_brain):
    mod = tmp_brain
    assert mod.classify_text("データ無いと言いすぎ") == "too_passive"
    assert mod.classify_text("response が長すぎ") == "too_verbose"
    assert mod.classify_text("AI 臭い") == "tone_mismatch"
    assert mod.classify_text("数字違ってる") == "factual_error"
    assert mod.classify_text("前回話した件") == "missed_context"
    assert mod.classify_text("scope 違う、海外じゃなく日本") == "wrong_default"
    assert mod.classify_text("ミラーリング失敗") == "mirroring_fail"
    assert mod.classify_text("特に問題なし") == "other"


def test_collect_audit_fails(tmp_brain):
    mod = tmp_brain
    today = datetime.now(JST).date()
    rec_list = [
        # bad note → too_passive
        {"ts": _ts(today), "verdict": "bad", "note": "データ無いと逃げてる",
         "user_query": "今月の売上", "bot_response": "..."},
        # fix → factual
        {"ts": _ts(today), "verdict": "fix", "note": "数字が違う",
         "user_query": "東京の客数", "bot_response": "..."},
        # good → skipped
        {"ts": _ts(today), "verdict": "good", "note": "OK"},
        # too old → skipped
        {"ts": _ts(today - timedelta(days=60)), "verdict": "bad", "note": "too_passive"},
    ]
    f = mod.AUDIT_DIR / "2026-05.jsonl"
    f.write_text("\n".join(json.dumps(r, ensure_ascii=False) for r in rec_list), encoding="utf-8")
    items = mod.collect_audit_fails(days=30)
    assert len(items) == 2
    patterns = [it["pattern"] for it in items]
    assert "too_passive" in patterns
    assert "factual_error" in patterns


def test_collect_feedback(tmp_brain):
    mod = tmp_brain
    today = datetime.now(JST).date()
    rec_list = [
        {"timestamp": _ts(today), "feedback": "AI 臭い", "trigger_msg": "雑談", "response": "..."},
        {"timestamp": _ts(today), "feedback": "", "trigger_msg": "x"},  # empty
        {"timestamp": _ts(today - timedelta(days=60)), "feedback": "too old"},
    ]
    f = mod.FEEDBACK_DIR / "2026-05.jsonl"
    f.write_text("\n".join(json.dumps(r, ensure_ascii=False) for r in rec_list), encoding="utf-8")
    items = mod.collect_feedback(days=30)
    assert len(items) == 1
    assert items[0]["pattern"] == "tone_mismatch"


def test_collect_learning_rq(tmp_brain):
    mod = tmp_brain
    today = datetime.now(JST).date()
    rec_list = [
        {"timestamp": _ts(today), "category": "response_quality",
         "insight": "[too_passive] データ無いと言いすぎ",
         "source_snippet": "USER: 売上は?"},
        {"timestamp": _ts(today), "category": "discovery",  # not response_quality, skip
         "insight": "別件"},
        {"timestamp": _ts(today), "category": "response_quality",
         "insight": "[factual_error] 数字違い", "source_snippet": "..."},
    ]
    f = mod.LEARNING_DIR / "2026-05.jsonl"
    f.write_text("\n".join(json.dumps(r, ensure_ascii=False) for r in rec_list), encoding="utf-8")
    items = mod.collect_learning_rq(days=30)
    assert len(items) == 2
    patterns = [it["pattern"] for it in items]
    assert "too_passive" in patterns
    assert "factual_error" in patterns


def test_aggregate_patterns_sort(tmp_brain):
    mod = tmp_brain
    items = [
        {"pattern": "too_passive", "ts": "2026-05-25"},
        {"pattern": "too_passive", "ts": "2026-05-24"},
        {"pattern": "too_passive", "ts": "2026-05-23"},
        {"pattern": "factual_error", "ts": "2026-05-25"},
        {"pattern": "tone_mismatch", "ts": "2026-05-25"},
    ]
    g = mod.aggregate_patterns(items)
    assert len(g["too_passive"]) == 3
    # ordered newest first
    assert g["too_passive"][0]["ts"] == "2026-05-25"


def test_generate_proposals_each_pattern(tmp_brain):
    mod = tmp_brain
    for pat in ("too_passive", "too_verbose", "tone_mismatch", "factual_error",
                "missed_context", "wrong_default", "mirroring_fail", "other"):
        text = mod.generate_proposals(pat, [{"ts": "x"}])
        assert pat in text or "対策案" in text
        assert "1 件" in text  # "(1 件)" 表記


def test_build_report_structure(tmp_brain):
    mod = tmp_brain
    by_pattern = {
        "too_passive": [{"source": "audit", "ts": "2026-05-25", "note": "..."}] * 3,
        "factual_error": [{"source": "learning", "ts": "2026-05-24", "note": "..."}] * 2,
    }
    report = mod.build_report(by_pattern, days=30, today=date(2026, 5, 26))
    assert "Style 逆流 週次レポート" in report
    assert "2026-05-26" in report
    assert "too_passive" in report
    assert "factual_error" in report
    assert "計 **5 件**" in report
    assert "pattern frequency" in report


def test_run_once_dry_run(tmp_brain, capsys):
    mod = tmp_brain
    today = datetime.now(JST).date()
    # seed 1 audit, 1 feedback, 1 learning
    (mod.AUDIT_DIR / "x.jsonl").write_text(
        json.dumps({"ts": _ts(today), "verdict": "bad", "note": "データ無い", "user_query": "売上", "bot_response": "..."}),
        encoding="utf-8",
    )
    result = mod.run_once(days=30, dry_run=True)
    assert result["dry_run"] is True
    assert result["items"] >= 1
    out = capsys.readouterr().out
    assert "Style 逆流" in out


def test_nav_includes_style_reflux():
    from services.review_dashboard import _nav
    html = _nav("/admin/review/learning", "test-token")
    assert "/admin/review/style-reflux" in html
    assert "Style逆流" in html


def test_render_style_reflux_page_no_data(tmp_brain):
    from services.review_dashboard import render_style_reflux_page
    html = render_style_reflux_page("test-token")
    assert "Style 逆流" in html
    assert "レポート無し" in html or "初回生成" in html


def test_render_style_reflux_page_with_report(tmp_brain):
    from services.review_dashboard import render_style_reflux_page
    # Create a fake report
    r = tmp_brain.OUT_DIR / "2026-05-25.md"
    r.write_text("# Style 逆流 週次レポート (2026-05-25、直近 30 日)\n\nsample content", encoding="utf-8")
    html = render_style_reflux_page("test-token")
    assert "2026-05-25" in html
    assert "sample content" in html


def test_cron_install_registers_style_reflux():
    src = (REPO_ROOT / "scripts" / "cron_install.sh").read_text()
    assert "style_reflux_cron.sh" in src
    assert "10 4 * * 1" in src  # 月曜 04:10
