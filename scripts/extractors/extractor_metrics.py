"""Extractor Metrics CLI — events.jsonl からサマリを出す

Usage:
    python3 extractor_metrics.py                  # 直近 7 日のサマリ
    python3 extractor_metrics.py --since 30d
    python3 extractor_metrics.py --since 24h
    python3 extractor_metrics.py --since all
    python3 extractor_metrics.py --extractor style_extractor   # 1 extractor だけ
    python3 extractor_metrics.py --tail 20         # 直近 20 件のイベントを raw 表示
    python3 extractor_metrics.py --failures        # 失敗イベントだけ抽出

events.jsonl の各行は:
    {"ts": "...", "extractor": "...", "event": "run_started"|"run_finished"|"run_failed"|...,
     "elapsed_sec": float, "items_written": int, "status": "ok|llm_failed|...", ...}
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent))

from _common import EVENTS_LOG  # type: ignore  # noqa: E402


def _parse_since(since: str) -> datetime | None:
    """'7d' / '24h' / '30d' / 'all' / ISO 日付 を datetime に。"""
    if since == "all":
        return None
    m = re.match(r"^(\d+)([dh])$", since)
    if m:
        n, unit = int(m.group(1)), m.group(2)
        if unit == "d":
            return datetime.now() - timedelta(days=n)
        if unit == "h":
            return datetime.now() - timedelta(hours=n)
    # ISO date 試行
    try:
        return datetime.fromisoformat(since)
    except ValueError:
        raise SystemExit(f"invalid --since: {since}. use Nd, Nh, all, or YYYY-MM-DD")


def _read_events(since_dt: datetime | None) -> list[dict]:
    if not EVENTS_LOG.exists():
        return []
    events = []
    with EVENTS_LOG.open("r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                rec = json.loads(line)
            except json.JSONDecodeError:
                continue
            ts = rec.get("ts", "")
            if since_dt is not None and ts:
                try:
                    if datetime.fromisoformat(ts) < since_dt:
                        continue
                except ValueError:
                    pass
            events.append(rec)
    return events


def _format_summary(events: list[dict], extractor_filter: str | None) -> str:
    """run_started/run_finished/run_failed をペアリングして人間可読サマリを作る。"""
    out_lines: list[str] = []

    # extractor 別に集計
    by_ext: dict[str, dict[str, Any]] = {}
    for e in events:
        ext = e.get("extractor", "unknown")
        if extractor_filter and ext != extractor_filter:
            continue
        d = by_ext.setdefault(ext, {
            "runs": 0,
            "successes": 0,
            "failures": 0,
            "no_patterns": 0,
            "no_fresh_raw": 0,
            "items_written": 0,
            "items_invalid": 0,
            "llm_failed": 0,
            "llm_schema_failed": 0,
            "elapsed_total": 0.0,
            "last_run_ts": "",
            "last_run_status": "",
        })
        if e.get("event") == "run_started":
            d["runs"] += 1
        elif e.get("event") == "run_finished":
            d["successes"] += 1
            d["elapsed_total"] += float(e.get("elapsed_sec", 0))
            d["items_written"] += int(e.get("items_written", 0) or 0)
            d["items_invalid"] += int(e.get("items_invalid", 0) or 0)
            status = e.get("status", "")
            if status == "no_patterns":
                d["no_patterns"] += 1
            elif status == "no_fresh_raw":
                d["no_fresh_raw"] += 1
            elif status == "llm_failed":
                d["llm_failed"] += 1
            elif status == "llm_schema_failed":
                d["llm_schema_failed"] += 1
            d["last_run_ts"] = e.get("ts", d["last_run_ts"])
            d["last_run_status"] = status or d["last_run_status"]
        elif e.get("event") == "run_failed":
            d["failures"] += 1
            d["last_run_ts"] = e.get("ts", d["last_run_ts"])
            d["last_run_status"] = "FAILED:" + e.get("error_class", "?")

    if not by_ext:
        return "(no events in window)"

    out_lines.append(f"{'extractor':<22} {'runs':>5} {'ok':>4} {'fail':>5} {'noraw':>6} {'nopat':>6} {'wrote':>6} {'invalid':>8} {'avg_s':>6} {'last_status':<20}")
    out_lines.append("─" * 100)
    for ext in sorted(by_ext.keys()):
        d = by_ext[ext]
        avg = d["elapsed_total"] / max(d["successes"], 1)
        last = d.get("last_run_status", "?")[:20]
        out_lines.append(
            f"{ext:<22} {d['runs']:>5} {d['successes']:>4} {d['failures']:>5} "
            f"{d['no_fresh_raw']:>6} {d['no_patterns']:>6} {d['items_written']:>6} "
            f"{d['items_invalid']:>8} {avg:>6.1f} {last:<20}"
        )

    # LLM の信頼性: llm_failed / llm_schema_failed が多いと LLM が壊れてる兆候
    llm_failures_total = sum(d["llm_failed"] + d["llm_schema_failed"] for d in by_ext.values())
    if llm_failures_total:
        out_lines.append("")
        out_lines.append(f"⚠️ LLM contract failures: {llm_failures_total} (--failures で詳細確認)")

    return "\n".join(out_lines)


def _format_failures(events: list[dict], extractor_filter: str | None) -> str:
    out_lines: list[str] = []
    fail_events = [
        e for e in events
        if e.get("event") in {"run_failed", "llm_call_failed", "llm_parse_failed", "llm_schema_failed", "binary_intrusion", "write_failed"}
        and (extractor_filter is None or e.get("extractor") == extractor_filter)
    ]
    if not fail_events:
        return "(no failures in window)"
    for e in fail_events[-50:]:  # 最新 50 件まで
        ts = e.get("ts", "?")
        ext = e.get("extractor", "?")
        ev = e.get("event", "?")
        rest = {k: v for k, v in e.items() if k not in {"ts", "extractor", "event"}}
        out_lines.append(f"{ts}  {ext:<22} {ev:<22} {json.dumps(rest, ensure_ascii=False)[:200]}")
    return "\n".join(out_lines)


def _format_tail(events: list[dict], n: int) -> str:
    out_lines: list[str] = []
    for e in events[-n:]:
        out_lines.append(json.dumps(e, ensure_ascii=False))
    return "\n".join(out_lines) or "(no events)"


def main() -> None:
    ap = argparse.ArgumentParser(description="Extractor metrics from events.jsonl")
    ap.add_argument("--since", default="7d", help="time window: 24h, 7d, 30d, all, or ISO date")
    ap.add_argument("--extractor", default=None, help="filter by extractor name")
    ap.add_argument("--tail", type=int, default=0, help="dump last N raw events instead of summary")
    ap.add_argument("--failures", action="store_true", help="show only failure events")
    args = ap.parse_args()

    since_dt = _parse_since(args.since)
    events = _read_events(since_dt)

    if not EVENTS_LOG.exists():
        print(f"events log not found: {EVENTS_LOG}")
        print("(extractor を 1 度も走らせていないか、別の BRAIN_APP_ROOT で実行している)")
        return

    print(f"events file: {EVENTS_LOG}")
    print(f"window: since {args.since} ({len(events)} events)")
    if args.extractor:
        print(f"filter: extractor={args.extractor}")
    print()

    if args.tail:
        print(_format_tail(events, args.tail))
    elif args.failures:
        print(_format_failures(events, args.extractor))
    else:
        print(_format_summary(events, args.extractor))


if __name__ == "__main__":
    main()
