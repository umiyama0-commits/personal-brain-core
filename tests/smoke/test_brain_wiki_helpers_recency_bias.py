"""smoke test: brain_wiki_helpers/recency_bias.py (★2026-05-22 Phase 1b)。

Pure function なので直接 import + test (= source 抽出 + exec の旧 idiom 不要)。
"""
from __future__ import annotations

from datetime import date, timedelta
from pathlib import Path

import pytest

from brain_wiki_helpers.recency_bias import (
    apply_recency_weight,
    _parse_last_updated,
    _recency_multiplier,
)


def _write_wiki(wiki_dir: Path, name: str, last_updated: str) -> None:
    (wiki_dir / f"{name}.md").write_text(
        f"---\nclone_visibility: public\nlast_updated: {last_updated}\n---\n# body\n",
        encoding="utf-8",
    )


# ─── _parse_last_updated 単体 ─────────────
@pytest.mark.smoke
def test_parse_last_updated_yyyy_mm_dd():
    content = "---\nlast_updated: 2026-05-22\n---\n# body\n"
    assert _parse_last_updated(content) == date(2026, 5, 22)


@pytest.mark.smoke
def test_parse_last_updated_missing():
    content = "---\nclone_visibility: public\n---\n# body\n"
    assert _parse_last_updated(content) is None


@pytest.mark.smoke
def test_parse_last_updated_no_frontmatter():
    assert _parse_last_updated("body only\n") is None


# ─── _recency_multiplier 単体 ─────────────
@pytest.mark.smoke
def test_multiplier_boundary_7():
    assert _recency_multiplier(0) == 1.05
    assert _recency_multiplier(7) == 1.05
    assert _recency_multiplier(8) == 1.02


@pytest.mark.smoke
def test_multiplier_boundary_30():
    assert _recency_multiplier(14) == 1.02
    assert _recency_multiplier(15) == 1.00
    assert _recency_multiplier(30) == 1.00
    assert _recency_multiplier(31) == 0.97


@pytest.mark.smoke
def test_multiplier_long_term():
    assert _recency_multiplier(90) == 0.97
    assert _recency_multiplier(180) == 0.93
    assert _recency_multiplier(365) == 0.85
    assert _recency_multiplier(366) == 0.70
    assert _recency_multiplier(1000) == 0.70


@pytest.mark.smoke
def test_multiplier_future_neutral():
    """負の値 (= 未来日付) は基準 1.00。"""
    assert _recency_multiplier(-1) == 1.00
    assert _recency_multiplier(-30) == 1.00


# ─── apply_recency_weight (= 統合) ─────────────
@pytest.mark.smoke
def test_apply_recency_boost_within_7_days(tmp_path):
    today = date.today()
    _write_wiki(tmp_path, "recent", (today - timedelta(days=3)).isoformat())
    _write_wiki(tmp_path, "old", (today - timedelta(days=500)).isoformat())
    hits = [
        {"source": "old.md", "content": "x", "distance": 0.3},
        {"source": "recent.md", "content": "y", "distance": 0.3},
    ]
    result = apply_recency_weight(hits, tmp_path)
    assert result[0]["source"] == "recent.md"
    assert result[1]["source"] == "old.md"


@pytest.mark.smoke
def test_apply_recency_no_meta_neutral(tmp_path):
    """last_updated 無い hit は基準 (×1.00) 扱い。"""
    today = date.today()
    (tmp_path / "no_meta.md").write_text("# body only\n", encoding="utf-8")
    _write_wiki(tmp_path, "recent", (today - timedelta(days=3)).isoformat())
    _write_wiki(tmp_path, "old", (today - timedelta(days=500)).isoformat())
    hits = [
        {"source": "old.md", "content": "x", "distance": 0.3},
        {"source": "no_meta.md", "content": "y", "distance": 0.3},
        {"source": "recent.md", "content": "z", "distance": 0.3},
    ]
    result = apply_recency_weight(hits, tmp_path)
    assert result[0]["source"] == "recent.md"
    assert result[1]["source"] == "no_meta.md"
    assert result[2]["source"] == "old.md"


@pytest.mark.smoke
def test_apply_recency_distance_aware(tmp_path):
    """同じ weight でも distance で順序が決まる。"""
    today = date.today()
    _write_wiki(tmp_path, "a", (today - timedelta(days=10)).isoformat())
    _write_wiki(tmp_path, "b", (today - timedelta(days=10)).isoformat())
    hits = [
        {"source": "a.md", "content": "x", "distance": 0.1},
        {"source": "b.md", "content": "y", "distance": 0.6},
    ]
    result = apply_recency_weight(hits, tmp_path)
    assert result[0]["source"] == "a.md"  # 高 sim 優先


@pytest.mark.smoke
def test_apply_recency_no_distance_uses_rank(tmp_path):
    """distance 無し → rank × weight。"""
    today = date.today()
    _write_wiki(tmp_path, "fresh", (today - timedelta(days=3)).isoformat())
    _write_wiki(tmp_path, "stale", (today - timedelta(days=500)).isoformat())
    hits = [
        {"source": "stale.md", "content": "x"},  # rank 1.0 × 0.70 = 0.70
        {"source": "fresh.md", "content": "y"},  # rank 0.5 × 1.05 = 0.525
    ]
    result = apply_recency_weight(hits, tmp_path)
    assert result[0]["source"] == "stale.md"  # 元 Chroma 順位優位


@pytest.mark.smoke
def test_apply_recency_empty_hits(tmp_path):
    assert apply_recency_weight([], tmp_path) == []


@pytest.mark.smoke
def test_brain_wiki_method_wraps_helper():
    """brain_wiki.py の _apply_recency_weight が helper を呼んでる。"""
    import re as _re
    from pathlib import Path as _Path
    REPO = _Path(__file__).resolve().parent.parent.parent
    src = (REPO / "brain_wiki.py").read_text(encoding="utf-8")
    # method 定義は残ってる
    assert "def _apply_recency_weight(self, hits" in src
    # helper import 経由
    assert "from brain_wiki_helpers.recency_bias import apply_recency_weight" in src
