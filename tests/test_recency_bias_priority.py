"""brain_wiki_helpers/recency_bias.py の rerank/recency 優先順位 test。

★2026-06-08 システム評価 1-4 (cross-check 3種で収束):
- (a) `_parse_last_updated` が `updated:` キーも読む (判断系 corpus の recency no-op 修正)
- (b) rerank_score がある時は relevance 主キー + near-tie のみ recency tie-break
      (明確な relevance 差を recency が覆さない = leapfrog 防止 / 拮抗は最新優先)
"""
import sys
from datetime import date, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from brain_wiki_helpers.recency_bias import (  # noqa: E402
    _parse_last_updated,
    apply_recency_weight,
    RERANK_TIEBREAK_EPS,
)


def _mk(wiki_dir: Path, name: str, updated_key: str, days_ago: int) -> str:
    """frontmatter 付き wiki file を作る。updated_key は 'updated' か 'last_updated'。
    返り値は source 相対パス。"""
    d = (date.today() - timedelta(days=days_ago)).strftime("%Y-%m-%d")
    (wiki_dir / name).write_text(
        f"---\n{updated_key}: {d}\nclone_visibility: public\n---\n# {name}\n本文",
        encoding="utf-8",
    )
    return name


# ─── (a) key parser fix ──────────────────────────────

def test_parse_reads_updated_key():
    """`updated:` キー (判断系 corpus が使う) を読めること。"""
    content = "---\nupdated: 2026-05-01\nconfidence: high\n---\n# x"
    assert _parse_last_updated(content) == date(2026, 5, 1)


def test_parse_reads_last_updated_key():
    """`last_updated:` も従来どおり読めること。"""
    content = "---\nlast_updated: 2026-05-02\n---\n# x"
    assert _parse_last_updated(content) == date(2026, 5, 2)


def test_parse_prefers_last_updated_when_both():
    """両方あれば last_updated: を優先。"""
    content = "---\nupdated: 2026-01-01\nlast_updated: 2026-05-03\n---\n# x"
    assert _parse_last_updated(content) == date(2026, 5, 3)


def test_parse_none_when_absent():
    content = "---\nconfidence: high\n---\n# x"
    assert _parse_last_updated(content) is None


# ─── (b) rerank path: relevance primary, recency tie-break ──────────────────────────────

def test_clear_relevance_not_leapfrogged_by_recency(tmp_path):
    """relevance 差が EPS 超なら、古いが高 relevance の doc が新しい低 relevance を抑える
    (= factual 安全、旧バグの leapfrog 防止)。"""
    old_hi = _mk(tmp_path, "old_high.md", "updated", days_ago=500)   # 古いが関連性高
    new_lo = _mk(tmp_path, "new_low.md", "updated", days_ago=2)      # 新しいが関連性低
    hits = [
        {"source": new_lo, "rerank_score": 0.50, "distance": 0.4},
        {"source": old_hi, "rerank_score": 0.90, "distance": 0.4},
    ]
    out = apply_recency_weight(hits, tmp_path)
    assert out[0]["source"] == old_hi  # 高 relevance が勝つ (recency に覆されない)


def test_near_tie_newest_wins(tmp_path):
    """relevance が EPS 以内の near-tie なら、最新 doc が勝つ (= judgment 最新優先)。"""
    old = _mk(tmp_path, "old.md", "updated", days_ago=300)
    new = _mk(tmp_path, "new.md", "updated", days_ago=1)
    # relevance はほぼ同点 (差 < EPS=0.05)、ただし古い方が僅かに高い
    hits = [
        {"source": old, "rerank_score": 0.82, "distance": 0.3},
        {"source": new, "rerank_score": 0.80, "distance": 0.3},
    ]
    assert (0.82 - 0.80) < RERANK_TIEBREAK_EPS
    out = apply_recency_weight(hits, tmp_path)
    assert out[0]["source"] == new  # near-tie は最新が勝つ


def test_dated_beats_undated_in_near_tie(tmp_path):
    """near-tie で、日付ありの doc が日付無しの doc より上位 (undated は最後尾)。"""
    dated = _mk(tmp_path, "dated.md", "updated", days_ago=5)
    (tmp_path / "undated.md").write_text("---\nconfidence: high\n---\n# u", encoding="utf-8")
    hits = [
        {"source": "undated.md", "rerank_score": 0.81},
        {"source": dated, "rerank_score": 0.80},
    ]
    out = apply_recency_weight(hits, tmp_path)
    assert out[0]["source"] == dated


# ─── backward-compat: distance path (no rerank_score) ──────────────────────────────

def test_distance_path_unchanged_when_no_rerank(tmp_path):
    """rerank_score 無し = 従来の (1-distance)×multiplier 経路。"""
    a = _mk(tmp_path, "a.md", "last_updated", days_ago=10)  # multiplier 1.02
    b = _mk(tmp_path, "b.md", "last_updated", days_ago=10)
    hits = [
        {"source": a, "distance": 0.5},   # sim 0.5
        {"source": b, "distance": 0.2},   # sim 0.8 → 高 score
    ]
    out = apply_recency_weight(hits, tmp_path)
    assert out[0]["source"] == b  # distance 小 (= 関連性高) が先頭


def test_empty_hits():
    assert apply_recency_weight([], Path("/tmp")) == []
