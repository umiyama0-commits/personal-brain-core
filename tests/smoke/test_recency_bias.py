"""smoke test: brain_wiki._apply_recency_weight (★2026-05-22 海山指示)。

vector search hits を last_updated 重み付けで rerank する。
brain_wiki.py 全体は重い import なので、関数を source 抽出 + exec で隔離 test。
"""
from __future__ import annotations

import re
import sys
import textwrap
from datetime import date, timedelta
from pathlib import Path
from unittest.mock import MagicMock

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent.parent


def _extract_recency_method() -> tuple[callable, callable]:
    """brain_wiki.py から _apply_recency_weight と _recency_multiplier を抽出。

    _apply_recency_weight は self/WIKI_DIR/re/Path に依存。
    隔離 test 用に必要なものを mock した namespace で exec。
    """
    src = (REPO_ROOT / "brain_wiki.py").read_text(encoding="utf-8")
    # _apply_recency_weight の定義範囲を取る
    start = src.find("    def _apply_recency_weight(self")
    assert start >= 0, "_apply_recency_weight not found"
    # 終端 = 次の def or class
    rest = src[start:]
    end_m = re.search(
        r"\n    @staticmethod\n|\n    def [a-zA-Z_]|\nclass |\ndef ",
        rest[len("    def _apply_recency_weight(self"):],
    )
    assert end_m, "end marker not found"
    func_src = rest[: len("    def _apply_recency_weight(self") + end_m.start() + 1]
    # dedent + self に依存しない形にする (= staticmethod 化)
    func_src = textwrap.dedent(func_src).replace(
        "def _apply_recency_weight(self, hits", "def apply_recency_weight(hits"
    )

    return func_src


@pytest.fixture
def apply_fn(tmp_path):
    """テスト用 wiki ディレクトリと exec で関数を取り出す。"""
    func_src = _extract_recency_method()
    wiki_dir = tmp_path / "wiki"
    wiki_dir.mkdir()

    ns = {
        "re": __import__("re"),
        "Path": Path,
        "WIKI_DIR": wiki_dir,
    }
    exec(func_src, ns)
    return ns["apply_recency_weight"], wiki_dir


def _write_wiki(wiki_dir: Path, name: str, last_updated: str) -> None:
    (wiki_dir / f"{name}.md").write_text(
        f"---\nclone_visibility: public\nlast_updated: {last_updated}\n---\n# body\n",
        encoding="utf-8",
    )


@pytest.mark.smoke
def test_recency_boost_within_7_days(apply_fn):
    """7 日以内 → ×1.05 で score boost。"""
    apply, wiki = apply_fn
    today = date.today()
    _write_wiki(wiki, "recent", (today - timedelta(days=3)).isoformat())
    _write_wiki(wiki, "old", (today - timedelta(days=500)).isoformat())

    hits = [
        # 元 Chroma 順は old → recent (distance 同じ程度)
        {"source": "old.md", "content": "x", "distance": 0.3},
        {"source": "recent.md", "content": "y", "distance": 0.3},
    ]
    sorted_hits = apply(hits)
    # recent (×1.05) が old (×0.70) より上にくる
    assert sorted_hits[0]["source"] == "recent.md"
    assert sorted_hits[1]["source"] == "old.md"


@pytest.mark.smoke
def test_recency_neutral_within_30_days(apply_fn):
    """14-30 日 → ×1.00 (基準)。30 日超え (×0.97) より優位。"""
    apply, wiki = apply_fn
    today = date.today()
    _write_wiki(wiki, "med", (today - timedelta(days=25)).isoformat())
    _write_wiki(wiki, "older", (today - timedelta(days=60)).isoformat())

    hits = [
        {"source": "older.md", "content": "x", "distance": 0.3},
        {"source": "med.md", "content": "y", "distance": 0.3},
    ]
    sorted_hits = apply(hits)
    assert sorted_hits[0]["source"] == "med.md"  # ×1.00
    assert sorted_hits[1]["source"] == "older.md"  # ×0.97


@pytest.mark.smoke
def test_recency_aging_180_365(apply_fn):
    """180-365 日 → ×0.85、365+ → ×0.70。階段で減衰。"""
    apply, wiki = apply_fn
    today = date.today()
    _write_wiki(wiki, "year_ish", (today - timedelta(days=300)).isoformat())
    _write_wiki(wiki, "very_old", (today - timedelta(days=500)).isoformat())

    hits = [
        {"source": "very_old.md", "content": "x", "distance": 0.3},  # ×0.70
        {"source": "year_ish.md", "content": "y", "distance": 0.3},  # ×0.85
    ]
    sorted_hits = apply(hits)
    assert sorted_hits[0]["source"] == "year_ish.md"
    assert sorted_hits[1]["source"] == "very_old.md"


@pytest.mark.smoke
def test_recency_missing_last_updated_neutral(apply_fn):
    """last_updated 無し → 基準 ×1.00 扱い (= 中間順位)。"""
    apply, wiki = apply_fn
    today = date.today()
    # 1 つだけ frontmatter なし
    (wiki / "no_meta.md").write_text("# body only\n", encoding="utf-8")
    _write_wiki(wiki, "recent", (today - timedelta(days=3)).isoformat())  # ×1.05
    _write_wiki(wiki, "old", (today - timedelta(days=500)).isoformat())  # ×0.70

    hits = [
        {"source": "old.md", "content": "x", "distance": 0.3},
        {"source": "no_meta.md", "content": "y", "distance": 0.3},
        {"source": "recent.md", "content": "z", "distance": 0.3},
    ]
    sorted_hits = apply(hits)
    # 順序: recent (×1.05) > no_meta (×1.00) > old (×0.70)
    assert sorted_hits[0]["source"] == "recent.md"
    assert sorted_hits[1]["source"] == "no_meta.md"
    assert sorted_hits[2]["source"] == "old.md"


@pytest.mark.smoke
def test_recency_distance_aware(apply_fn):
    """distance が大きい (= 似てない) hit は weight 同じでも下位。"""
    apply, wiki = apply_fn
    today = date.today()
    _write_wiki(wiki, "a", (today - timedelta(days=10)).isoformat())  # ×1.02
    _write_wiki(wiki, "b", (today - timedelta(days=10)).isoformat())  # ×1.02

    # 同じ weight でも distance で決まる
    hits = [
        {"source": "a.md", "content": "x", "distance": 0.1},  # 高 sim
        {"source": "b.md", "content": "y", "distance": 0.6},  # 低 sim
    ]
    sorted_hits = apply(hits)
    assert sorted_hits[0]["source"] == "a.md"  # 高 sim 優先
    assert sorted_hits[1]["source"] == "b.md"


@pytest.mark.smoke
def test_recency_no_distance_uses_rank(apply_fn):
    """distance 無しの hits は元 rank × multiplier で並び。"""
    apply, wiki = apply_fn
    today = date.today()
    _write_wiki(wiki, "fresh", (today - timedelta(days=3)).isoformat())  # ×1.05
    _write_wiki(wiki, "stale", (today - timedelta(days=500)).isoformat())  # ×0.70

    # 元順位は stale 先、fresh 後 (= Chroma が出した順)
    hits = [
        {"source": "stale.md", "content": "x"},  # rank 1.0 × 0.70 = 0.70
        {"source": "fresh.md", "content": "y"},  # rank 0.5 × 1.05 = 0.525
    ]
    sorted_hits = apply(hits)
    # rank × weight で stale が勝つケース、ただし weight 差が大きいので
    # 結果は実装の境界次第。stale は 0.70、fresh は 0.525 → stale 先
    # これが正解 (= Chroma の semantic 強い時は古くても優先される)
    assert sorted_hits[0]["source"] == "stale.md"


@pytest.mark.smoke
def test_recency_empty_hits(apply_fn):
    """空 hits は空のまま返る。"""
    apply, _ = apply_fn
    assert apply([]) == []


@pytest.mark.smoke
def test_recency_boundary_7_days(apply_fn):
    """7 日ちょうどは ×1.05 (= 境界包含)。"""
    apply, wiki = apply_fn
    today = date.today()
    _write_wiki(wiki, "day7", (today - timedelta(days=7)).isoformat())  # ×1.05
    _write_wiki(wiki, "day8", (today - timedelta(days=8)).isoformat())  # ×1.02

    hits = [
        {"source": "day8.md", "content": "x", "distance": 0.3},
        {"source": "day7.md", "content": "y", "distance": 0.3},
    ]
    sorted_hits = apply(hits)
    assert sorted_hits[0]["source"] == "day7.md"


@pytest.mark.smoke
def test_brain_wiki_integration_callsite():
    """brain_wiki.py 内で _apply_recency_weight が vector loop 直前で呼ばれている。"""
    src = (REPO_ROOT / "brain_wiki.py").read_text(encoding="utf-8")
    # 呼び出し箇所が存在
    assert "self._apply_recency_weight(hits)" in src
    # 定義
    assert "def _apply_recency_weight(self, hits" in src
