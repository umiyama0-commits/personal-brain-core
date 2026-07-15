"""bot_monitor_daily.py — Plan C v2 Step 6 monitor 集計 (★2026-05-24 海山指示 Tier 2 D)

Strategy reviewer 指摘「monitor 後回しにすると Phase 2 がやった気で終わる」反映、
bot_events.jsonl から daily 集計 (= category 分布 / 応答 length 分布 / fallback 率 /
few-shot leak / context leak) を出す CLI + 集計 helper。

Usage:
    python3 scripts/bot_monitor_daily.py                     # 24h サマリ
    python3 scripts/bot_monitor_daily.py --since 7d           # 7 日
    python3 scripts/bot_monitor_daily.py --json               # endpoint 用 JSON
    python3 scripts/bot_monitor_daily.py --compare 24h:7d     # 24h vs 7d diff

設計:
  既存 bot_events.py / bot_metrics.py の idiom を踏襲、re-implement しない:
  - turn_finished event のみ filter
  - inline metadata (= category / lines / fewshot_leak / context_leak / retrieval_fallback) を集計
  - retrieval_fallback_triggered event は別途集計 (= 既存 c4bc056 の event)
  - context_prefix_leak event は critical alert として別 list

cron:
  03:50 daily で scripts/clone_cron.sh から呼ぶ (= 03:30 regression / 03:45 hallucination の後、04:00 eval-baseline の前)
"""
from __future__ import annotations

import argparse
import json
import sys
from collections import Counter
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent))
from bot_events import iter_events, parse_since  # type: ignore  # noqa: E402


# ─── length bucket 定義 (= eval set v1 と同基準) ──────────────────────────
_LENGTH_BUCKETS = [
    ("短文(1-3行)", 1, 3),
    ("中文(3-5行)", 3, 5),
    ("長文(5-10行)", 5, 10),
    ("超長文(10+行)", 10, 9999),
]


def _length_bucket(lines: int) -> str:
    for name, lo, hi in _LENGTH_BUCKETS:
        if lo <= lines < hi:
            return name
    return "短文(1-3行)" if lines < 1 else "超長文(10+行)"


def aggregate(events: list[dict]) -> dict:
    """events から monitor 指標を集計。

    Returns:
      {
        "n_turn_started": int,
        "n_turn_finished": int,
        "n_turn_failed": int,
        "n_retrieval_fallback": int,
        "n_context_prefix_leak": int,        # critical
        "category_dist": {"挨拶": N, "雑談": N, ...},
        "length_dist": {"短文(1-3行)": N, ...},
        "fewshot_leak_count": int,
        "fallback_rate_pct": float,
        "context_leak_examples": [...]       # critical alert sample
      }
    """
    n_started = 0
    n_finished = 0
    n_failed = 0
    n_retrieval_fallback = 0
    n_context_leak_event = 0
    category_counter: Counter = Counter()
    length_counter: Counter = Counter()
    n_fewshot_leak = 0
    context_leak_examples: list[dict] = []

    for e in events:
        ev = e.get("event", "")
        comp = e.get("component", "")
        if ev == "turn_started":
            n_started += 1
        elif ev == "turn_finished":
            n_finished += 1
            # inline metadata
            cat = e.get("category", "")
            if cat:
                category_counter[cat] += 1
            lines = e.get("lines")
            if lines is not None:
                try:
                    length_counter[_length_bucket(int(lines))] += 1
                except Exception:
                    pass
            if e.get("fewshot_leak"):
                n_fewshot_leak += 1
            if e.get("context_leak"):
                # turn_finished に context_leak=True のものはここで集計
                # (= context_prefix_leak event は別途、二重集計しない)
                pass
        elif ev == "turn_failed":
            n_failed += 1
        elif ev == "retrieval_fallback_triggered":
            n_retrieval_fallback += 1
        elif ev == "context_prefix_leak":
            n_context_leak_event += 1
            context_leak_examples.append({
                "ts": e.get("ts", ""),
                "query": (e.get("query") or "")[:80],
                "response_head": (e.get("response_head") or "")[:120],
            })

    fallback_rate = round(
        100.0 * n_retrieval_fallback / n_started, 2
    ) if n_started > 0 else 0.0

    return {
        "n_turn_started": n_started,
        "n_turn_finished": n_finished,
        "n_turn_failed": n_failed,
        "n_retrieval_fallback": n_retrieval_fallback,
        "n_context_prefix_leak": n_context_leak_event,
        "category_dist": dict(category_counter.most_common()),
        "length_dist": {b[0]: length_counter.get(b[0], 0) for b in _LENGTH_BUCKETS},
        "fewshot_leak_count": n_fewshot_leak,
        "fallback_rate_pct": fallback_rate,
        "context_leak_examples": context_leak_examples[:5],
    }


def _format_text(agg: dict, label: str = "") -> str:
    lines = [f"=== {label or 'monitor summary'} ==="]
    lines.append(
        f"turns: started={agg['n_turn_started']}, finished={agg['n_turn_finished']}, "
        f"failed={agg['n_turn_failed']}"
    )
    lines.append(
        f"retrieval_fallback: {agg['n_retrieval_fallback']} 件 "
        f"({agg['fallback_rate_pct']}% / started、想定 5-15%)"
    )
    lines.append(
        f"context_prefix_leak: {agg['n_context_prefix_leak']} 件 "
        f"(★critical、0 以外なら fix #1 漏れ要調査)"
    )
    lines.append(f"fewshot_leak: {agg['fewshot_leak_count']} 件 (★逐語複写検出、daily 5+ で alert)")
    lines.append("")
    lines.append("category 分布:")
    total_cat = sum(agg["category_dist"].values()) or 1
    for cat, n in agg["category_dist"].items():
        pct = round(100.0 * n / total_cat, 1)
        lines.append(f"  {cat}: {n} 件 ({pct}%)")
    lines.append("")
    lines.append("length 分布:")
    total_len = sum(agg["length_dist"].values()) or 1
    for bucket, n in agg["length_dist"].items():
        pct = round(100.0 * n / total_len, 1)
        lines.append(f"  {bucket}: {n} 件 ({pct}%)")

    if agg["context_leak_examples"]:
        lines.append("")
        lines.append("★context_prefix_leak 例 (critical):")
        for ex in agg["context_leak_examples"]:
            lines.append(f"  {ex['ts']}: query={ex['query']}")
            lines.append(f"    response_head={ex['response_head']}")
    return "\n".join(lines)


def evaluate_alerts(agg: dict) -> list[str]:
    """★2026-06-07 エージェント評価: 集計値の threshold 判定。breach メッセージ list を返す。

    従来は集計を JSON 出力するだけで critical 指標 (context_prefix_leak 等) が誰にも飛ばず
    埋もれていた (= silent-fail)。本関数 + --alert で LINE Push に繋ぐ。
    """
    alerts: list[str] = []
    if agg.get("n_context_prefix_leak", 0) > 0:
        alerts.append(f"🔴 context_prefix_leak {agg['n_context_prefix_leak']} 件 (critical、context 注入剥がし漏れ)")
    if agg.get("fewshot_leak_count", 0) >= 5:
        alerts.append(f"🟠 fewshot_leak {agg['fewshot_leak_count']} 件 (逐語複写、daily 5+ 閾値超)")
    fr = agg.get("fallback_rate_pct", 0)
    if agg.get("n_turn_started", 0) >= 20 and (fr < 5 or fr > 15):
        alerts.append(f"🟠 fallback_rate {fr}% (想定 5-15% 外)")
    if agg.get("n_turn_started", 0) == 0:
        alerts.append("⚠️ turn_started 0 件 — 監視盲目 (BRAIN_ROOT/event path) or 完全無 traffic の疑い")
    return alerts


def main():
    parser = argparse.ArgumentParser(description="bot 応答 monitor (= Plan C v2 Step 6)")
    parser.add_argument("--since", default="24h",
                        help="集計範囲 (例: 24h / 7d / 30d / all, default 24h)")
    parser.add_argument("--json", action="store_true", help="JSON 出力 (endpoint 用)")
    parser.add_argument("--compare", default="",
                        help="2 期間 diff 表示 (例: '24h:7d')")
    parser.add_argument("--alert", action="store_true",
                        help="threshold breach 時 LINE Push (★cron monitor-daily で使用)")
    args = parser.parse_args()

    if args.compare:
        a, b = args.compare.split(":", 1)
        ev_a = list(iter_events(since_sec=parse_since(a)))
        ev_b = list(iter_events(since_sec=parse_since(b)))
        agg_a = aggregate(ev_a)
        agg_b = aggregate(ev_b)
        if args.json:
            print(json.dumps({
                "period_a": args.compare.split(":")[0],
                "period_b": args.compare.split(":")[1],
                "a": agg_a, "b": agg_b,
            }, ensure_ascii=False, indent=2))
        else:
            print(_format_text(agg_a, label=f"period a = {a}"))
            print()
            print(_format_text(agg_b, label=f"period b = {b}"))
        return

    events = list(iter_events(since_sec=parse_since(args.since)))
    agg = aggregate(events)
    if args.alert:
        alerts = evaluate_alerts(agg)
        if alerts:
            try:
                from clone_improve_lib import line_push  # type: ignore
                line_push(f"🤖 bot monitor daily ({args.since})\n" + "\n".join(alerts))
            except Exception as e:
                print(f"alert push failed: {e}", file=sys.stderr)
    if args.json:
        print(json.dumps(agg, ensure_ascii=False, indent=2))
    else:
        print(_format_text(agg, label=f"--since {args.since}"))


if __name__ == "__main__":
    main()
