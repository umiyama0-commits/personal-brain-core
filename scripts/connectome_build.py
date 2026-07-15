#!/usr/bin/env python3
"""連想想起グラフを既存ログから offline 構築する(Phase 1 / eval-only、本番非接触)。

data/brain/bot_events/events.jsonl の retrieval/wiki_context イベントが各クエリで
採用した非コア wiki doc 群(recall フィールド)を共起ペアとして集計し、
重み付き共起グラフ(= 連想配線のたたき台)を JSON 出力する。

★既存ログを読むだけ。retrieval の hot path には一切触れない(Reviewer 確認済、brain_wiki.py:4014)。
★core doc は毎クエリ常在=ハブノイズになるため、recall(非コア)のみで共起を取るのが好都合。
★quality 重み付け(共起×回答品質)は Phase 1.5(judge ログと join)。本スクリプトは素の共起。

使い方:
  python3 scripts/connectome_build.py                         # 既定パスから
  python3 scripts/connectome_build.py --events <p> --out <p>  # パス指定
  python3 scripts/connectome_build.py --min-edge 2            # 単発共起ノイズ除去

出力に hub ノード上位・エッジ重み分布を併記(共起信号の健全性チェック=ハブ汚染の早期検知)。
"""
from __future__ import annotations

import argparse
import json
import sys
from collections import Counter
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from scripts.connectome import build_cooccurrence_graph  # noqa: E402


def _iter_recall_events(events_path: Path):
    """retrieval/wiki_context イベントの recall(>=2件)だけを順に返す。"""
    with events_path.open(encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                r = json.loads(line)
            except json.JSONDecodeError:
                continue
            if r.get("component") == "retrieval" and r.get("event") == "wiki_context":
                recall = r.get("recall") or []
                if len(recall) >= 2:
                    yield recall


def main() -> int:
    ap = argparse.ArgumentParser(description="連想想起グラフを既存 bot_events ログから構築")
    ap.add_argument("--events", default="data/brain/bot_events/events.jsonl")
    ap.add_argument("--out", default="data/brain/connectome/cooccurrence_v1.json")
    ap.add_argument("--min-edge", type=float, default=2.0,
                    help="この重み未満のエッジを捨てる(単発共起=偶然のノイズ除去)")
    ap.add_argument("--top-hubs", type=int, default=20)
    args = ap.parse_args()

    events_path = Path(args.events)
    if not events_path.exists():
        print(f"[connectome_build] events ログが見つかりません: {events_path}", file=sys.stderr)
        print("  → 本番(Mac Studio)の data/brain/bot_events/events.jsonl で実行するか --events で指定。",
              file=sys.stderr)
        return 1

    events = [{"doc_ids": recall, "quality": 1.0} for recall in _iter_recall_events(events_path)]
    if not events:
        print("[connectome_build] recall>=2 のイベントが 0 件。まだ共起データが不足。", file=sys.stderr)
        return 1

    g = build_cooccurrence_graph(events)

    # min-edge 未満を刈る(偶然の単発共起を落とす)
    pruned: dict[str, dict[str, float]] = {}
    for a, nbrs in g.items():
        kept = {b: w for b, w in nbrs.items() if w >= args.min_edge}
        if kept:
            pruned[a] = kept

    # 健全性レポート: hub ノード(次数)上位・エッジ重み分布
    degree: Counter = Counter()
    weights: list[float] = []
    for a, nbrs in pruned.items():
        degree[a] = len(nbrs)
        weights.extend(nbrs.values())
    n_nodes = len(pruned)
    n_edges = sum(len(n) for n in pruned.values()) // 2  # 対称なので半分

    print(f"[connectome_build] events(recall>=2)={len(events)}  nodes={n_nodes}  edges={n_edges}  "
          f"(min-edge={args.min_edge})")
    if weights:
        ws = sorted(weights)
        print(f"  edge weight: min={ws[0]:.0f}  median={ws[len(ws) // 2]:.0f}  max={ws[-1]:.0f}")
    print(f"  top {args.top_hubs} hub docs (高次数=多くと共起=ハブ汚染候補):")
    for doc, deg in degree.most_common(args.top_hubs):
        print(f"    {deg:4d}  {doc}")

    out_path = Path(args.out)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(json.dumps(pruned, ensure_ascii=False), encoding="utf-8")
    print(f"[connectome_build] 書き出し: {out_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
