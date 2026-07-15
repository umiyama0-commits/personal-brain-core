"""tests/test_persona_v3_dimensions.py — ★2026-07-03 v3「脳の複製」拡張の回帰テスト。

海山指示「人格の補完をもっとディープに。仕事だけではなく、人間の脳みそのduplicate」。
8→16 次元拡張・category 配線・/diary 取込 (episodic memory) を固定する。
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

import alignment_interview as ai  # noqa: E402

V3_DIMS = {"episodic_memory", "family_private", "humor", "shadow",
           "taste_daily", "money_personal", "body_health", "inner_voice"}
V3_CATEGORIES = {"episode", "family", "humor", "shadow", "taste",
                 "money", "body", "inner_voice"}


def test_v3_dimensions_present_and_complete():
    ids = {d["id"] for d in ai.DIMENSIONS}
    assert V3_DIMS <= ids
    assert len(ai.DIMENSIONS) == 16
    for d in ai.DIMENSIONS:
        # 全次元が probes + wiki_targets を持つ (voice/gap-questions が両方に依存)
        assert d["probes"] and d["wiki_targets"], d["id"]


def test_v3_categories_route_to_private_interview():
    for cat in V3_CATEGORIES:
        rel = ai._CATEGORY_WIKI[cat]
        # 生活者次元は必ず interview/ (= private 固定ヘッダで書かれる) 配下
        assert rel.startswith("interview/"), (cat, rel)
        assert cat in ai._CATEGORY_TITLE


def test_extract_prompt_mentions_v3_categories_and_guards():
    p = ai.EXTRACT_PROMPT
    for cat in V3_CATEGORIES:
        assert cat in p, f"EXTRACT_PROMPT に {cat} が無い"
    for dim in V3_DIMS:
        assert dim in p, f"dims_with_substance 候補に {dim} が無い"
    # 家族「本人」の機微は書かないガードが残っていること
    assert "家族「本人」" in p or "家族本人" in p


def test_coverage_autofills_new_dimensions(tmp_path, monkeypatch):
    # 旧 coverage (8次元時代) を読んでも新次元が自動補完される
    monkeypatch.setattr(ai, "ALIGN_DIR", tmp_path)
    monkeypatch.setattr(ai, "COVERAGE_FILE", tmp_path / "interview_coverage.json")
    import json
    old = {"version": "alignment-interview-v1", "dimensions": {"biography": {
        "session_count": 3, "depth_score": 4, "last_explored": "2026-06-01", "notes": ""}},
        "session_log": []}
    (tmp_path / "interview_coverage.json").write_text(json.dumps(old), encoding="utf-8")
    cov = ai.load_coverage()
    for dim in V3_DIMS:
        assert dim in cov["dimensions"], dim
        assert cov["dimensions"][dim]["depth_score"] == 0
    assert cov["dimensions"]["biography"]["depth_score"] == 4  # 既存は保持


def test_diary_command_is_dispatched_from_main(monkeypatch):
    """★reviewer B1 回帰ガード: /diary が main.py の admin dispatch tuple に居ること。

    library 層 (record_diary_entry) が完璧でも、dispatch gate に prefix が無いと
    LINE からは一度も届かない (v3 初版で実際に起きた last-mile 欠落)。source-level で固定。
    """
    src = (REPO_ROOT / "main.py").read_text(encoding="utf-8")
    import re
    m = re.search(r'user_message\.startswith\(\((.*?)\)\)', src, re.DOTALL)
    assert m, "admin dispatch tuple が見つからない"
    assert '"/diary"' in m.group(1), "/diary が dispatch tuple に無い (B1 再発)"
    # handler 側は厳密 prefix (typo /diaryy を wiki 書込にしない)
    cmd_src = (REPO_ROOT / "brain_commands.py").read_text(encoding="utf-8")
    assert 'message == "/diary" or message.startswith("/diary ")' in cmd_src


def test_record_diary_entry_writes_private_episode(tmp_path, monkeypatch):
    monkeypatch.setattr(ai, "WIKI_DIR", tmp_path / "wiki")
    monkeypatch.setattr(ai, "RAW_DIR", tmp_path / "raw" / "alignment_voice")
    monkeypatch.setattr(ai, "ALIGN_DIR", tmp_path / "alignment")
    monkeypatch.setattr(ai, "EXTRACTED_DIR", tmp_path / "alignment" / "interview_extracted")
    monkeypatch.setattr(ai, "COVERAGE_FILE", tmp_path / "alignment" / "cov.json")

    r = ai.record_diary_entry("テスト: 天神で若い店長が客の名前を全部覚えてた")
    assert r["ok"] and r["file"] == "interview/episodes.md"
    ep = (tmp_path / "wiki" / "interview" / "episodes.md").read_text(encoding="utf-8")
    assert "clone_visibility: private" in ep          # 社員クローン非露出
    assert "天神で若い店長" in ep                       # 原文のまま (蒸留なし)
    raw = list((tmp_path / "raw" / "diary").glob("*.md"))
    assert raw, "raw/diary/ に原本が無い"
    # coverage の episodic_memory に加点されている
    import json
    cov = json.loads((tmp_path / "alignment" / "cov.json").read_text(encoding="utf-8"))
    assert cov["dimensions"]["episodic_memory"]["depth_score"] >= 1

    # 空文字は拒否
    assert ai.record_diary_entry("  ")["ok"] is False
