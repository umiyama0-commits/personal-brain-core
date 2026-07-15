"""embodiment_indexer の単体テスト。"""
from __future__ import annotations

import importlib
import json
import sys
from datetime import date
from pathlib import Path

import pytest


def _reload_embodiment(common):
    if "embodiment_indexer" in sys.modules:
        importlib.reload(sys.modules["embodiment_indexer"])
    else:
        import embodiment_indexer  # noqa: F401
    return sys.modules["embodiment_indexer"]


def _valid_entry():
    return {
        "id_slug": "audio-test-001",
        "modality": "audio",
        "external_path": "s3://test/sample.m4a",
        "duration_sec": 100,
        "context": "test",
        "emotional_state": "engaged",
        "speaking_rate": "200 syll/min",
        "pitch_range": "100-200 Hz",
        "notable_patterns": ["x"],
        "training_eligible": "yes",
        "training_eligible_reason": "test reason",
        "recorded_at": "2026-04-15",
        "clone_visibility": "private",
        "exit_visibility": "internal",
    }


def test_validate_entry_valid(common, brain_root):
    em = _reload_embodiment(common)
    ok, reason = em._validate_entry(_valid_entry())
    assert ok, reason


def test_validate_entry_invalid_modality(common, brain_root):
    em = _reload_embodiment(common)
    e = _valid_entry()
    e["modality"] = "voice"  # invalid
    ok, _ = em._validate_entry(e)
    assert not ok


def test_validate_entry_missing_external_path(common, brain_root):
    em = _reload_embodiment(common)
    e = _valid_entry()
    del e["external_path"]
    ok, _ = em._validate_entry(e)
    assert not ok


def test_validate_entry_invalid_emotion(common, brain_root):
    em = _reload_embodiment(common)
    e = _valid_entry()
    e["emotional_state"] = "happy"
    ok, _ = em._validate_entry(e)
    assert not ok


def test_validate_entry_missing_training_reason(common, brain_root):
    em = _reload_embodiment(common)
    e = _valid_entry()
    e["training_eligible_reason"] = ""
    ok, _ = em._validate_entry(e)
    assert not ok


def test_binary_intrusion_scan_clean(common, brain_root):
    em = _reload_embodiment(common)
    intruders = em._scan_for_binary_intrusion()
    assert intruders == []


def test_binary_intrusion_scan_finds_wav(common, brain_root):
    em = _reload_embodiment(common)
    em_dir = brain_root / "data" / "brain" / "wiki" / "embodiment"
    em_dir.mkdir(parents=True, exist_ok=True)
    (em_dir / "leak.wav").write_bytes(b"fake wav header")
    intruders = em._scan_for_binary_intrusion()
    assert len(intruders) == 1
    assert intruders[0].name == "leak.wav"


def test_binary_intrusion_finds_video(common, brain_root):
    em = _reload_embodiment(common)
    em_dir = brain_root / "data" / "brain" / "wiki" / "embodiment"
    (em_dir / "v.mp4").write_bytes(b"fake mp4")
    (em_dir / "v.mov").write_bytes(b"fake mov")
    intruders = em._scan_for_binary_intrusion()
    assert len(intruders) == 2


def test_validate_entry_external_path_into_wiki_with_binary(common, brain_root):
    """external_path が wiki/ 配下を指してて拡張子がバイナリなら拒否"""
    em = _reload_embodiment(common)
    em_dir = brain_root / "data" / "brain" / "wiki" / "embodiment"
    bad_local = em_dir / "should_not_be_here.wav"
    bad_local.write_bytes(b"x")  # 実在させて relative_to が成立するように
    e = _valid_entry()
    e["external_path"] = str(bad_local)
    ok, reason = em._validate_entry(e)
    assert not ok
    assert "wiki/" in reason
