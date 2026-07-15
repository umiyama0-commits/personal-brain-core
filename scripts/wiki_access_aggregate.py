#!/usr/bin/env python3
"""wiki_access_aggregate.py — Phase 0 集計 (ADR docs/decisions/2026-06-01-tiered-memory-architecture.md)

bot_events.jsonl の `retrieval/wiki_context` イベント (= `_read_wiki_state_public_compact`
が read-only で記録する採用 wiki) を集計し:
- どの「非 core」wiki が deep recall で採用されたか (時間減衰つき頻度 = warm slot 候補)
- context サイズ分布 p50/p95 (= G3 の global CORE_BUDGET 設計 baseline)
- query intent 分布
を `data/brain/wiki_access_counts.json` に書き出し + 人間用レポートを print する。

read-only。warm slot 投入是非 (ADR G6: 手 curate registry に regression で勝てるか) を
事実で判断するための材料。bot 挙動・コストは一切変えない。
Mac Studio で実行 (events.jsonl は本番にある)。

使い方:
  python3 scripts/wiki_access_aggregate.py [--half-life-days 30] [--since-days 90]
"""
from __future__ import annotations

import argparse
import json
import os
from collections import defaultdict
from datetime import datetime
from pathlib import Path
from typing import Optional


def _percentile(xs: list, p: float) -> int:
    if not xs:
        return 0
    xs = sorted(xs)
    k = max(0, min(len(xs) - 1, int(round((p / 100.0) * (len(xs) - 1)))))
    return int(xs[k])


def aggregate(
    brain_root,
    half_life_days: float = 30.0,
    since_days: float = 90.0,
    now: Optional[datetime] = None,
    write: bool = True,
) -> dict:
    """events.jsonl を集計して dict を返す (write=True で wiki_access_counts.json も書く)。

    now を渡すと age 計算が決定論的になる (test 用)。
    """
    brain_root = Path(brain_root)
    events = brain_root / "bot_events" / "events.jsonl"
    out_path = brain_root / "wiki_access_counts.json"
    if now is None:
        now = datetime.now()

    decayed: dict = defaultdict(float)   # 非core wiki path -> 減衰つき採用回数
    raw: dict = defaultdict(int)         # 非core wiki path -> 生採用回数
    sizes: list = []                     # total_chars 分布 (CORE_BUDGET baseline)
    intents: dict = defaultdict(int)
    n_turns = 0

    if events.exists():
        for line in events.read_text(encoding="utf-8", errors="replace").splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                r = json.loads(line)
            except Exception:
                continue
            if r.get("component") != "retrieval" or r.get("event") != "wiki_context":
                continue
            try:
                ts = datetime.fromisoformat(r.get("ts", ""))
            except Exception:
                continue
            age_days = (now - ts).total_seconds() / 86400.0
            if age_days < 0 or age_days > since_days:
                continue
            n_turns += 1
            intents[r.get("query_intent", "?")] += 1
            tc = r.get("total_chars")
            if isinstance(tc, (int, float)):
                sizes.append(int(tc))
            w = 0.5 ** (age_days / half_life_days) if half_life_days > 0 else 1.0
            for p in (r.get("recall") or []):
                decayed[p] += w
                raw[p] += 1

    ranked = sorted(decayed.items(), key=lambda kv: kv[1], reverse=True)
    out = {
        "generated_at": now.isoformat(timespec="seconds"),
        "n_turns": n_turns,
        "half_life_days": half_life_days,
        "since_days": since_days,
        "context_chars_p50": _percentile(sizes, 50),
        "context_chars_p95": _percentile(sizes, 95),
        "intent_distribution": dict(intents),
        "recall_decayed": {k: round(v, 3) for k, v in ranked},
        "recall_raw": dict(raw),
    }
    if write:
        try:
            out_path.write_text(json.dumps(out, ensure_ascii=False, indent=1), encoding="utf-8")
        except Exception as e:  # noqa: BLE001
            print(f"(warn) write failed: {e}")
    return out


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--half-life-days", type=float, default=30.0)
    ap.add_argument("--since-days", type=float, default=90.0)
    args = ap.parse_args()

    brain_root = Path(os.getenv("BRAIN_ROOT", "/app/data/brain"))
    out = aggregate(brain_root, args.half_life_days, args.since_days)

    print(f"=== wiki access 計測 (Phase 0) — {out['n_turns']} turns 集計 ===")
    if out["n_turns"] == 0:
        print("(まだ retrieval/wiki_context イベントが無い。bot を数日回してから再実行。)")
        return
    print(
        f"context_chars: p50={out['context_chars_p50']} p95={out['context_chars_p95']} "
        f"(= global CORE_BUDGET 設計の baseline)"
    )
    print(f"intent 分布: {out['intent_distribution']}")
    print("--- 非 core wiki の deep recall 頻度 top 25 (時間減衰つき / 生回数) ---")
    for k, v in list(out["recall_decayed"].items())[:25]:
        print(f"  {v:8.2f}  (raw {out['recall_raw'].get(k, 0):4d})  {k}")
    print(f"\n→ {Path(os.getenv('BRAIN_ROOT', '/app/data/brain')) / 'wiki_access_counts.json'}")
    print("判断材料: 安定して上位の非 core wiki = warm slot 候補。")
    print("低 traffic で raw が小さい/順位が毎回ブレるなら warm slot はまだ早い (G6 = 手 curate 継続)。")


if __name__ == "__main__":
    main()
