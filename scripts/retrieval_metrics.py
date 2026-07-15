#!/usr/bin/env python3
"""retrieval_metrics.py — retrieval 評価の標準 metric (Recall@k / nDCG@k / MRR / Precision@k)。

★2026-06-08 システム評価 Retrieval CRITICAL: 「retrieval 精度を測る golden eval が存在しない」
穴を塞ぐための計測コア。bot 応答品質 (eval_runner) とは分離して「検索が正解 doc を上位に
返せているか」を客観計測する (= 世界標準は retrieval と generation を分けて測る)。

純関数のみ・外部依存なし → 完全にローカルで unit test 可能。harness (retrieval_eval.py) が
これを呼ぶ。ranked_ids は doc-level (重複除去済) を前提とする。
"""
from __future__ import annotations

import math
from typing import Optional, Sequence


def dedup_preserve_order(ids: Sequence[str]) -> list:
    """ranked list の重複を除去 (= doc の初出順位を採用)。chunk 単位 hit を doc 単位に畳む用。"""
    seen = set()
    out = []
    for x in ids:
        if x not in seen:
            seen.add(x)
            out.append(x)
    return out


def recall_at_k(ranked_ids: Sequence[str], gold_ids: Sequence[str], k: int) -> Optional[float]:
    """top-k に含まれた gold の割合。gold 空なら None (= 未定義、集計から除外)。"""
    gold = set(gold_ids)
    if not gold:
        return None
    topk = set(ranked_ids[:k])
    return len(topk & gold) / len(gold)


def precision_at_k(ranked_ids: Sequence[str], gold_ids: Sequence[str], k: int) -> float:
    """top-k のうち gold だった割合。"""
    if k <= 0:
        return 0.0
    topk = ranked_ids[:k]
    if not topk:
        return 0.0
    gold = set(gold_ids)
    return len([1 for x in topk if x in gold]) / len(topk)


def mrr(ranked_ids: Sequence[str], gold_ids: Sequence[str]) -> float:
    """Mean Reciprocal Rank の 1 クエリ分 = 最初の gold が出た順位の逆数 (無ければ 0)。"""
    gold = set(gold_ids)
    for i, did in enumerate(ranked_ids, start=1):
        if did in gold:
            return 1.0 / i
    return 0.0


def dcg_at_k(ranked_ids: Sequence[str], gold_ids: Sequence[str], k: int) -> float:
    """binary relevance (gold=1) の DCG@k。"""
    gold = set(gold_ids)
    s = 0.0
    for i, did in enumerate(ranked_ids[:k], start=1):
        if did in gold:
            s += 1.0 / math.log2(i + 1)
    return s


def ndcg_at_k(ranked_ids: Sequence[str], gold_ids: Sequence[str], k: int) -> Optional[float]:
    """nDCG@k (binary relevance)。gold 空なら None。

    end-to-end RAG 品質と最も相関する metric とされる (= 順位の良さを連続評価)。
    """
    gold = set(gold_ids)
    if not gold:
        return None
    dcg = dcg_at_k(ranked_ids, gold_ids, k)
    ideal_hits = min(len(gold), k)
    idcg = sum(1.0 / math.log2(i + 1) for i in range(1, ideal_hits + 1))
    return dcg / idcg if idcg > 0 else 0.0


def aggregate(per_query: list, ks: Sequence[int] = (1, 3, 5, 10)) -> dict:
    """per_query = [{"ranked": [...], "gold": [...]}] から平均 metric を出す。

    gold 空のクエリは recall/nDCG の平均から除外 (None を弾く)。MRR は全件平均。
    """
    out: dict = {"n_queries": len(per_query)}

    def _mean(vals):
        vals = [v for v in vals if v is not None]
        return round(sum(vals) / len(vals), 4) if vals else None

    for k in ks:
        out[f"recall@{k}"] = _mean([recall_at_k(q["ranked"], q["gold"], k) for q in per_query])
        out[f"ndcg@{k}"] = _mean([ndcg_at_k(q["ranked"], q["gold"], k) for q in per_query])
    out["mrr"] = _mean([mrr(q["ranked"], q["gold"]) for q in per_query])
    # gold が 1 件も top-10 に入らなかった (= 完全 miss) クエリ数 = retrieval の穴
    out["full_miss"] = sum(
        1 for q in per_query if q["gold"] and recall_at_k(q["ranked"], q["gold"], 10) == 0.0
    )
    return out
