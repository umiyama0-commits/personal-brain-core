"""drift_detector の単体テスト。

LLM を呼ばないので httpx mock 不要。
"""
from __future__ import annotations

import importlib
from datetime import date, timedelta
from pathlib import Path

import pytest


def _reload_drift(common):
    """drift_detector も brain_root に追従させて再 import。"""
    import sys
    if "drift_detector" in sys.modules:
        importlib.reload(sys.modules["drift_detector"])
    else:
        import drift_detector  # noqa: F401
    return sys.modules["drift_detector"]


def _write_pattern(layer_dir: Path, slug: str, last_date: str, field: str = "last_updated"):
    p = layer_dir / f"{slug}.md"
    p.write_text(
        f"---\ntype: style_pattern\nid: {slug}\n"
        f"category: vocabulary\ncontext: casual_chat\n"
        f"pattern: テスト用パターン\nevidence: [test.md]\n"
        f"counter_evidence: []\nconfidence: medium\n"
        f"{field}: {last_date}\n"
        f"clone_visibility: public\nexit_visibility: public\n---\n# body\n",
        encoding="utf-8",
    )
    return p


def test_drift_detector_clean_state(common, brain_root):
    drift = _reload_drift(common)
    style_dir = brain_root / "data" / "brain" / "wiki" / "style"
    today = date.today().isoformat()
    _write_pattern(style_dir, "style-fresh-001", today)

    result = drift.detect()
    assert result.drift == [], "fresh pattern should not be flagged"
    assert result.broken_pointers == []


def test_drift_detector_finds_stale(common, brain_root):
    drift = _reload_drift(common)
    style_dir = brain_root / "data" / "brain" / "wiki" / "style"
    stale_date = (date.today() - timedelta(days=120)).isoformat()
    _write_pattern(style_dir, "style-stale-001", stale_date)

    result = drift.detect()
    assert len(result.drift) == 1
    assert result.drift[0].layer == "style"
    assert result.drift[0].days_old >= 120
    assert result.drift[0].threshold == 90


def test_drift_detector_respects_threshold(common, brain_root):
    """89 日 (style 閾値 90 日 未満) は flag されない、91 日は flag される"""
    drift = _reload_drift(common)
    style_dir = brain_root / "data" / "brain" / "wiki" / "style"
    _write_pattern(style_dir, "style-near-89", (date.today() - timedelta(days=89)).isoformat())
    _write_pattern(style_dir, "style-over-91", (date.today() - timedelta(days=91)).isoformat())

    result = drift.detect()
    flagged_ids = {f.file_path.stem for f in result.drift}
    assert "style-near-89" not in flagged_ids
    assert "style-over-91" in flagged_ids


def test_drift_detector_reflex_threshold_60(common, brain_root):
    drift = _reload_drift(common)
    reflex_dir = brain_root / "data" / "brain" / "wiki" / "reflex"
    _write_pattern(reflex_dir, "reflex-old-001", (date.today() - timedelta(days=70)).isoformat(), field="last_observed")

    result = drift.detect()
    reflex_findings = [f for f in result.drift if f.layer == "reflex"]
    assert len(reflex_findings) == 1
    assert reflex_findings[0].threshold == 60


def test_drift_detector_apply_writes_log_and_pending(common, brain_root):
    drift = _reload_drift(common)
    style_dir = brain_root / "data" / "brain" / "wiki" / "style"
    _write_pattern(style_dir, "style-stale-002", (date.today() - timedelta(days=200)).isoformat())

    result = drift.detect()
    appended_log = drift.append_drift_log(result.drift)
    assert appended_log == 1

    drift_log = brain_root / "data" / "brain" / "meta" / "drift_log.md"
    assert drift_log.exists()
    log_text = drift_log.read_text(encoding="utf-8")
    assert "style-stale-002" in log_text

    appended_pending = drift.append_pending_questions(result.drift)
    assert appended_pending >= 1
    pending = brain_root / "data" / "brain" / "audit" / "pending_questions.md"
    assert pending.exists()
    assert "Q-001" in pending.read_text(encoding="utf-8")


def test_drift_detector_mark_files_idempotent(common, brain_root):
    drift = _reload_drift(common)
    style_dir = brain_root / "data" / "brain" / "wiki" / "style"
    p = _write_pattern(style_dir, "style-mark-001", (date.today() - timedelta(days=200)).isoformat())

    result = drift.detect()
    assert drift.mark_files(result.drift) == 1
    assert "validation: drift_pending" in p.read_text(encoding="utf-8")
    # 二度目は no-op (既に marked なので 0 件)
    assert drift.mark_files(result.drift) == 0
