"""retrieval_metrics の純関数 test (Recall@k / nDCG@k / MRR)。

★2026-06-08 システム評価 Retrieval CRITICAL: golden eval の計測コア。既知値で固定する。
"""
import math
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))

from retrieval_metrics import (  # noqa: E402
    recall_at_k, precision_at_k, mrr, ndcg_at_k, dedup_preserve_order, aggregate,
)


def test_recall_hit_at_rank1():
    r = ["a", "b", "c", "d"]
    assert recall_at_k(r, ["a"], 1) == 1.0
    assert recall_at_k(r, ["a"], 3) == 1.0


def test_recall_miss():
    assert recall_at_k(["b", "c", "d"], ["a"], 10) == 0.0


def test_recall_partial_multi_gold():
    r = ["a", "x", "b"]
    assert recall_at_k(r, ["a", "b"], 1) == 0.5   # top-1 に 1/2
    assert recall_at_k(r, ["a", "b"], 3) == 1.0   # top-3 に 2/2


def test_recall_none_when_no_gold():
    assert recall_at_k(["a"], [], 5) is None


def test_mrr_rank2():
    assert mrr(["b", "a", "c"], ["a"]) == 0.5


def test_mrr_miss_is_zero():
    assert mrr(["b", "c"], ["a"]) == 0.0


def test_ndcg_perfect_at_rank1():
    assert ndcg_at_k(["a", "b"], ["a"], 5) == 1.0


def test_ndcg_rank2_single_gold():
    # gold が rank2: dcg = 1/log2(3), idcg = 1/log2(2) = 1
    val = ndcg_at_k(["x", "a", "y"], ["a"], 5)
    assert abs(val - (1 / math.log2(3))) < 1e-9


def test_ndcg_two_gold():
    # ranked=[a,x,b], gold={a,b}: dcg=1/log2(2)+1/log2(4)=1.5; idcg=1/log2(2)+1/log2(3)
    val = ndcg_at_k(["a", "x", "b"], ["a", "b"], 3)
    idcg = 1 + 1 / math.log2(3)
    assert abs(val - (1.5 / idcg)) < 1e-9


def test_precision_at_k():
    assert precision_at_k(["a", "b", "c"], ["a", "c"], 3) == 2 / 3


def test_dedup_preserve_order():
    assert dedup_preserve_order(["a", "b", "a", "c", "b"]) == ["a", "b", "c"]


def test_aggregate_basic():
    per_q = [
        {"ranked": ["a", "b"], "gold": ["a"]},     # recall@1=1, mrr=1
        {"ranked": ["x", "y", "g"], "gold": ["g"]},  # recall@1=0, recall@3=1, mrr=1/3
        {"ranked": ["n", "m"], "gold": ["z"]},     # full miss
    ]
    agg = aggregate(per_q, ks=(1, 3))
    assert agg["n_queries"] == 3
    assert agg["recall@1"] == round((1.0 + 0.0 + 0.0) / 3, 4)
    assert agg["recall@3"] == round((1.0 + 1.0 + 0.0) / 3, 4)
    assert agg["full_miss"] == 1  # 3つ目は top-10 に gold 無し


def test_aggregate_excludes_no_gold_from_recall():
    per_q = [
        {"ranked": ["a"], "gold": ["a"]},
        {"ranked": ["b"], "gold": []},  # gold 無し → recall 平均から除外
    ]
    agg = aggregate(per_q, ks=(1,))
    assert agg["recall@1"] == 1.0  # gold ありの 1 件のみで平均
