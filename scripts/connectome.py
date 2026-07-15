"""Connectome + plasticity — 生物学的な脳に倣った連想記憶レイヤ (Phase 1 / eval-only).

生物の脳の 2 原理を retrieval/記憶レイヤに持ち込む実験モジュール:
  1. コネクトーム = 機能は「重み付き有向グラフ(配線)を想起時にたどる」ことで生まれる
     → spreading_activation(): 通常 retrieval の seed から配線を 1〜2 hop たどり連想想起。
  2. シナプス可塑性 = 一緒に使われた結合は強まり(fire together, wire together)、
     使われない結合は減衰・刈り込み(Hebbian LTP/LTD + pruning)
     → hebbian_update(): 共起 × 回答品質でエッジ強化、全体を僅かに減衰、閾値未満を刈る。

★Phase 1 は eval / offline 専用。本番 retrieval の挙動は一切変えない。
  本番投入(retrieval パイプライン変更 = CLAUDE.md 1.15(b))は
  gold 質問 regression + cross-check + A/B + 海山 sign-off の後。
  設計と判断ゲートの詳細: docs/decisions/2026-06-20-connectome-plasticity-memory.md

純粋関数のみ(外部依存・I/O・chromadb 非依存)。tests/test_connectome.py で隔離検証。
"""
from __future__ import annotations

from collections import defaultdict
from typing import Iterable, Mapping, Sequence

# node_id -> {neighbor_id: weight}。有向(対称にするかは build 側で選択)。
Graph = dict[str, dict[str, float]]


def build_cooccurrence_graph(
    events: Iterable[Mapping],
    *,
    ids_key: str = "doc_ids",
    quality_key: str = "quality",
    min_quality: float = 0.0,
    symmetric: bool = True,
) -> Graph:
    """共起グラフを構築する。

    各 event = {ids_key: [doc_id, ...], quality_key: float}.
    同一 event 内で共起した doc ペアのエッジ重みに quality を加算
    (= 良い回答に一緒に寄与したものほど強く結ぶ = fire together, wire together)。

    - 同一 event 内の重複 id は 1 回に畳む(自己ループ・二重計上の防止)。
    - quality 既定 1.0(品質情報が無い純粋共起にも使える)。
    - min_quality 未満の event は無視。
    """
    g: Graph = defaultdict(lambda: defaultdict(float))
    for ev in events:
        ids = list(dict.fromkeys(ev.get(ids_key) or []))  # 順序保持 dedup
        q = float(ev.get(quality_key, 1.0))
        if q < min_quality:
            continue
        for i in range(len(ids)):
            for j in range(i + 1, len(ids)):
                a, b = ids[i], ids[j]
                g[a][b] += q
                if symmetric:
                    g[b][a] += q
    return {k: dict(v) for k, v in g.items()}


def merge_graphs(*graphs: Graph, weights: Sequence[float] | None = None) -> Graph:
    """複数グラフ(例: 組織構造グラフ + 共起グラフ)を重み付きで合成する。"""
    if weights is None:
        weights = [1.0] * len(graphs)
    if len(weights) != len(graphs):
        raise ValueError("weights の数が graphs と一致しません")
    out: Graph = defaultdict(lambda: defaultdict(float))
    for g, w in zip(graphs, weights):
        for a, nbrs in g.items():
            for b, wt in nbrs.items():
                out[a][b] += wt * w
    return {k: dict(v) for k, v in out.items()}


def spreading_activation(
    graph: Graph,
    seeds: Sequence[str],
    *,
    hops: int = 1,
    decay: float = 0.5,
    top_n: int | None = None,
    min_activation: float = 0.0,
    include_seeds: bool = False,
) -> list[tuple[str, float]]:
    """seeds から配線をたどって活性を伝播させる(連想想起)。

    seed の初期活性 1.0。hop ごとに neighbor へ activation*weight*decay^hop を加算。
    複数経路は合算。seeds は既定で結果から除外(retrieval が既に保持しているため)。

    返り値: (node_id, activation) を活性の降順。top_n / min_activation で絞れる。
    決定論的(乱数なし)。
    """
    activation: dict[str, float] = defaultdict(float)
    frontier: dict[str, float] = {s: 1.0 for s in seeds}
    seed_set = set(seeds)
    for hop in range(max(0, hops)):
        nxt: dict[str, float] = defaultdict(float)
        for node, act in frontier.items():
            for nbr, w in graph.get(node, {}).items():
                nxt[nbr] += act * w * (decay ** (hop + 1))
        for n, a in nxt.items():
            activation[n] += a
        frontier = nxt
        if not frontier:
            break
    if not include_seeds:
        for s in seed_set:
            activation.pop(s, None)
    ranked = sorted(activation.items(), key=lambda kv: (-kv[1], kv[0]))
    ranked = [(n, a) for n, a in ranked if a >= min_activation]
    if top_n is not None:
        ranked = ranked[:top_n]
    return ranked


def hebbian_update(
    graph: Graph,
    events: Iterable[Mapping],
    *,
    lr: float = 0.1,
    decay: float = 0.01,
    prune_below: float = 1e-3,
    ids_key: str = "doc_ids",
    quality_key: str = "quality",
) -> Graph:
    """可塑性の 1 ステップ(純粋関数 — 新しい graph を返す)。

    ★offline シミュレーション/研究専用。本番の live write-path には接続しない。
      理由: (1) chromadb 並行書込禁止 §1.5(bot 稼働中の自己書換は SIGSEGV scar の禁止パターン)、
            (2) 共起強化 × LLM-judge 報酬 の 2 重自己強化ループ(rich-get-richer + 報酬ハック)。
      ADR: docs/decisions/2026-06-20-connectome-plasticity-memory.md。
      なお decay は Hebbian の暴走を抑える homeostatic 項として内蔵済(純粋強化のみにしない)。

    1. LTD/忘却: 既存の全エッジを (1-decay) 倍に減衰。
    2. LTP/強化: 各 event 内の共起ペアを lr*quality だけ強化(対称)。
    3. Pruning: prune_below 未満のエッジを刈り込み、孤立ノードを除去。

    → 使われ続ける結合は維持・強化、使われない結合は静かに消える(シナプス刈り込み)。
    """
    g: Graph = defaultdict(lambda: defaultdict(float))
    for a, nbrs in graph.items():
        for b, w in nbrs.items():
            decayed = w * (1.0 - decay)
            if decayed != 0.0:
                g[a][b] = decayed
    for ev in events:
        ids = list(dict.fromkeys(ev.get(ids_key) or []))
        q = float(ev.get(quality_key, 1.0))
        for i in range(len(ids)):
            for j in range(i + 1, len(ids)):
                a, b = ids[i], ids[j]
                g[a][b] += lr * q
                g[b][a] += lr * q
    pruned: Graph = {}
    for a, nbrs in g.items():
        kept = {b: w for b, w in nbrs.items() if w >= prune_below}
        if kept:
            pruned[a] = kept
    return pruned
