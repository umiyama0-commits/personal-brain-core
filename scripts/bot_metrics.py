"""
bot_metrics.py — bot 応答系の events.jsonl からサマリを出す (★2026-05-21 追加)

Usage:
    python3 scripts/bot_metrics.py                  # 直近 7 日のサマリ
    python3 scripts/bot_metrics.py --since 24h
    python3 scripts/bot_metrics.py --since 30d
    python3 scripts/bot_metrics.py --since all
    python3 scripts/bot_metrics.py --component clone_respond   # 1 component
    python3 scripts/bot_metrics.py --tail 20                   # 直近 20 件 raw
    python3 scripts/bot_metrics.py --failures                  # 失敗だけ
    python3 scripts/bot_metrics.py --by-user                   # user 別 traffic

設計:
  events.jsonl の各行は:
    {"ts": "...", "component": "clone_respond|clone_memory_update|sleep_time|...",
     "event": "turn_started|turn_finished|turn_failed|turn_skipped",
     "elapsed_ms": int, "user_id": "...", "model": "...", "response_chars": int, ...}

  この CLI は p50/p95 latency, 失敗率, user 別 traffic を grep/jq 無しで
  ローカルから 1 コマンドで見るためのもの。Extractor 用の同等 CLI が
  scripts/extractors/extractor_metrics.py にあり、それと同じ idiom。
"""
from __future__ import annotations

import argparse
import json
import sys
from collections import defaultdict
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent))
from bot_events import _events_log_path, iter_events, parse_since  # type: ignore  # noqa: E402

# litellm alias (= litellm_config.yaml model_name) → 課金 model 名 (PRICE_TABLE key)。
# ★2026-05-29: services/usage_analytics.COST_MODEL_ALIASES と同期させること
#   (daily LINE push と Web dashboard の cost 数値を一致させるため)。
#   events の model field が alias 文字列の場合に PRICE_TABLE key へ解決する。
_COST_ALIASES = {
    "smart": "claude-opus-4-8",
    "smart-legacy": "claude-opus-4-20250514",
    "smart-fallback": "gpt-4o",
    "contextualize": "claude-haiku-4-5",
    "fast": "gpt-4o",
    "default": "gpt-4o",
    "smart-gpt": "gpt-5.4",
    "smart-gpt-pro": "gpt-5.4-pro",
    "fast-gpt": "gpt-5.4-mini",
    "code": "gpt-5.4-pro",
    "code-max": "gpt-5-pro",
}


def _percentile(values: list[float], pct: float) -> float:
    """単純 percentile (interp なし、ソート後 index)"""
    if not values:
        return 0.0
    s = sorted(values)
    k = max(0, min(len(s) - 1, int(round(pct / 100 * (len(s) - 1)))))
    return s[k]


def _read_events(since_sec: int | None) -> list[dict]:
    return list(iter_events(since_sec=since_sec))


def _format_summary(events: list[dict], component_filter: str | None) -> str:
    """component 別の runs / ok / fail / skip / p50/p95 latency サマリ。"""
    out_lines: list[str] = []
    by_comp: dict[str, dict[str, Any]] = {}

    for e in events:
        comp = e.get("component", "unknown")
        if component_filter and comp != component_filter:
            continue
        d = by_comp.setdefault(comp, {
            "started": 0,
            "ok": 0,
            "failed": 0,
            "skipped": 0,
            "retrieval_fallback": 0,  # ★2026-05-23 Plan C v2 Step 5 集計、5-15% 目安
            "latencies_ms": [],
            "response_chars_total": 0,
            "n_with_resp_chars": 0,
            "last_ts": "",
            "last_status": "",
        })
        ev = e.get("event")
        if ev == "turn_started":
            d["started"] += 1
        elif ev == "turn_finished":
            d["ok"] += 1
            lat = e.get("elapsed_ms")
            if lat is not None:
                try:
                    d["latencies_ms"].append(float(lat))
                except Exception:
                    pass
            rc = e.get("response_chars")
            if rc is not None:
                try:
                    d["response_chars_total"] += int(rc)
                    d["n_with_resp_chars"] += 1
                except Exception:
                    pass
            d["last_ts"] = e.get("ts", d["last_ts"])
            d["last_status"] = e.get("status", "") or "ok"
        elif ev == "turn_failed":
            d["failed"] += 1
            d["last_ts"] = e.get("ts", d["last_ts"])
            d["last_status"] = "FAIL:" + (e.get("error_class") or "?")
        elif ev in ("turn_skipped", "user_skipped"):
            d["skipped"] += 1
        elif ev == "retrieval_fallback_triggered":
            # ★2026-05-23 Plan C v2 Step 5: retrieval 0 件 fallback 発動を別軸で集計
            d["retrieval_fallback"] += 1

    if not by_comp:
        return "(no events in window)"

    out_lines.append(
        f"{'component':<22} {'start':>6} {'ok':>5} {'fail':>5} {'skip':>5} {'rfb':>5} "
        f"{'p50_ms':>7} {'p95_ms':>7} {'avg_resp':>9} {'last_status':<14}"
    )
    out_lines.append("─" * 100)
    for comp in sorted(by_comp.keys()):
        d = by_comp[comp]
        lats = d["latencies_ms"]
        p50 = int(_percentile(lats, 50)) if lats else 0
        p95 = int(_percentile(lats, 95)) if lats else 0
        avg_resp = int(d["response_chars_total"] / d["n_with_resp_chars"]) if d["n_with_resp_chars"] else 0
        last = (d["last_status"] or "?")[:14]
        # rfb = retrieval_fallback (= Plan C v2 Step 5)
        out_lines.append(
            f"{comp:<22} {d['started']:>6} {d['ok']:>5} {d['failed']:>5} {d['skipped']:>5} {d.get('retrieval_fallback', 0):>5} "
            f"{p50:>7} {p95:>7} {avg_resp:>9} {last:<14}"
        )

    total_failed = sum(d["failed"] for d in by_comp.values())
    if total_failed:
        out_lines.append("")
        out_lines.append(f"⚠️  total failures in window: {total_failed} (--failures で詳細)")

    return "\n".join(out_lines)


def _format_by_user(events: list[dict], top_n: int = 30) -> str:
    """user_id 別の turn count + 平均 latency。heavy user 検出用。"""
    by_user: dict[str, dict[str, Any]] = defaultdict(lambda: {
        "turns": 0,
        "latencies": [],
        "fail": 0,
    })
    for e in events:
        uid = (e.get("user_id") or "")
        if not uid:
            continue
        ev = e.get("event")
        if ev == "turn_finished":
            by_user[uid]["turns"] += 1
            try:
                by_user[uid]["latencies"].append(float(e.get("elapsed_ms", 0)))
            except Exception:
                pass
        elif ev == "turn_failed":
            by_user[uid]["fail"] += 1

    if not by_user:
        return "(no user events)"

    rows = sorted(by_user.items(), key=lambda kv: -kv[1]["turns"])[:top_n]
    out = [f"{'user':<12} {'turns':>6} {'fail':>5} {'avg_ms':>7} {'p95_ms':>7}"]
    out.append("─" * 50)
    for uid, d in rows:
        lats = d["latencies"]
        avg = int(sum(lats) / len(lats)) if lats else 0
        p95 = int(_percentile(lats, 95)) if lats else 0
        out.append(f"{uid[:12]:<12} {d['turns']:>6} {d['fail']:>5} {avg:>7} {p95:>7}")
    return "\n".join(out)


def _format_failures(events: list[dict], component_filter: str | None) -> str:
    out: list[str] = []
    fail_events = [
        e for e in events
        if e.get("event") == "turn_failed"
        and (component_filter is None or e.get("component") == component_filter)
    ]
    if not fail_events:
        return "(no failures in window)"
    for e in fail_events[-50:]:
        ts = e.get("ts", "?")
        comp = e.get("component", "?")
        rest = {k: v for k, v in e.items() if k not in ("ts", "component", "event")}
        out.append(f"{ts}  {comp:<22} {json.dumps(rest, ensure_ascii=False)[:200]}")
    return "\n".join(out)


def _format_tail(events: list[dict], n: int) -> str:
    out: list[str] = []
    for e in events[-n:]:
        out.append(json.dumps(e, ensure_ascii=False))
    return "\n".join(out) or "(no events)"


def _format_cost_summary(events: list[dict]) -> str:
    """LLM cost 集計 (★2026-05-23 LEE §4.2)。

    bot_events.jsonl の `usage` フィールドから model 別 token 数を集計し、
    PRICE_TABLE と乗算して USD/日を概算する。前日比 +30% 以上で警告マーク。

    PRICE_TABLE は public API 価格 (LiteLLM proxy 経由のため厳密値ではない、目安)。
    本物のコスト確定値は LiteLLM /spend/logs から取得 (= /api/cost-investigation)、
    こちらは「daily LINE Push 用の即値」。
    """
    # USD per 1M token (★ 2026-05-23 時点の public 価格、要メンテ)
    PRICE_TABLE = {
        # Anthropic — ★2026-05-29 修正: Opus 4.7/4.8 は $5/$25/$0.5 (公式 pricing)。
        # 旧 $15/$75/$1.5 は Opus 4.1 単価の誤適用 (全 Opus call を 3x 過大計上)。
        # 4-20250514 (= 旧 Opus 4 2025-05) は実際に $15/$75 系なので据置。
        "claude-opus-4-8": {"input": 5.00, "output": 25.00, "cache_read": 0.50, "cache_write": 6.25},
        "claude-opus-4-7": {"input": 5.00, "output": 25.00, "cache_read": 0.50, "cache_write": 6.25},
        "claude-opus-4-20250514": {"input": 15.00, "output": 75.00, "cache_read": 1.50, "cache_write": 18.75},
        "claude-haiku-4-5": {"input": 1.00, "output": 5.00, "cache_read": 0.10, "cache_write": 1.25},
        # OpenAI
        "gpt-4o": {"input": 2.50, "output": 10.00, "cache_read": 1.25},
        "gpt-5.4": {"input": 10.00, "output": 40.00, "cache_read": 2.50},
        "gpt-5.4-pro": {"input": 30.00, "output": 120.00},
        "gpt-5.4-mini": {"input": 0.50, "output": 2.00, "cache_read": 0.125},
        "gpt-5-pro": {"input": 60.00, "output": 240.00},
        "gpt-5-codex": {"input": 10.00, "output": 40.00},
    }
    _price_keys_by_len = sorted(PRICE_TABLE, key=len, reverse=True)
    # 日付別 × モデル別の集計
    daily_usage: dict[str, dict] = {}  # date -> model -> {input, output, cache_read, calls}
    for e in events:
        if e.get("event") != "turn_finished":
            continue
        ts = e.get("ts", "")
        if not ts or len(ts) < 10:
            continue
        date = ts[:10]
        usage = e.get("usage") or {}
        if not isinstance(usage, dict):
            continue
        model = e.get("model") or "?"
        # litellm proxy 経由なら "anthropic/claude-opus-4-7" 等の形式 → suffix のみ取る
        if "/" in model:
            model = model.split("/", 1)[1]
        # alias ("smart") / dated 変種 ("gpt-4o-2024-08-06") を PRICE_TABLE key へ解決。
        # ★2026-05-29: usage_analytics._cost_canonical と同方針 (daily push と dashboard の
        #   model 別数値を一致させる)。未解決なら下の fallback 価格に落ちる。
        if model not in PRICE_TABLE:
            if model in _COST_ALIASES:
                model = _COST_ALIASES[model]
            else:
                for _k in _price_keys_by_len:
                    if model.startswith(_k):
                        model = _k
                        break
        d = daily_usage.setdefault(date, {}).setdefault(
            model, {"input": 0, "output": 0, "cache_read": 0, "cache_write": 0, "calls": 0}
        )
        # ★2026-05-29: LiteLLM/OpenAI 形式の prompt_tokens は cache_read+cache_write を含む合算値。
        # input bucket には cache を引いた uncached のみ積む (二重課金回避、usage_analytics と同算術)。
        _pt = int(usage.get("prompt_tokens") or 0)
        _it = int(usage.get("input_tokens") or 0)
        _cr = int(usage.get("cache_read_input_tokens") or 0)
        _cw = int(usage.get("cache_creation_input_tokens") or 0)
        _ptd = usage.get("prompt_tokens_details")
        if not _cr and isinstance(_ptd, dict):
            _cr = int(_ptd.get("cached_tokens") or 0)
        # pt あり=合算(OpenAI/LiteLLM、cache 込み)、pt 無し=Anthropic-native(it=uncached)。
        # pt<cr+cw の時は pt が cache 抜き値とみなし cache を足し戻す (version 差耐性)。
        if _pt:
            _total_in = _pt if _pt >= _cr + _cw else _pt + _cr + _cw
        else:
            _total_in = _it + _cr + _cw
        d["input"] += max(0, _total_in - _cr - _cw)
        d["output"] += int(usage.get("completion_tokens") or usage.get("output_tokens") or 0)
        d["cache_read"] += _cr
        d["cache_write"] += _cw
        d["calls"] += 1

    if not daily_usage:
        return "(no usage data in events.jsonl — 'usage' フィールドが空、bot 側で記録ロジック未実装)"

    # 日別 USD 集計
    daily_usd: dict[str, float] = {}
    for date, by_model in daily_usage.items():
        total = 0.0
        for model, tk in by_model.items():
            price = PRICE_TABLE.get(model, {"input": 5.0, "output": 15.0})  # fallback 推定値
            total += tk["input"] * price["input"] / 1_000_000
            total += tk["output"] * price["output"] / 1_000_000
            total += tk.get("cache_read", 0) * price.get("cache_read", price["input"]) / 1_000_000
            total += tk.get("cache_write", 0) * price.get("cache_write", price["input"] * 1.25) / 1_000_000
        daily_usd[date] = round(total, 2)

    lines = ["# LiteLLM Cost Summary (★ 2026-05-23 LEE §4.2、PRICE_TABLE は目安、要メンテ)", ""]
    dates_sorted = sorted(daily_usd.keys())
    prev_usd = None
    for d in dates_sorted:
        usd = daily_usd[d]
        delta = ""
        if prev_usd is not None and prev_usd > 0:
            pct = (usd - prev_usd) / prev_usd * 100
            mark = " ⚠️ +30%超" if pct > 30 else ""
            delta = f"  (前日比 {pct:+.0f}%{mark})"
        lines.append(f"  {d}: ${usd:7.2f}{delta}")
        prev_usd = usd
    lines.append("")
    # model 別 (最終日)
    last_date = dates_sorted[-1] if dates_sorted else None
    if last_date:
        lines.append(f"## {last_date} model 別:")
        for model, tk in sorted(daily_usage[last_date].items()):
            price = PRICE_TABLE.get(model, {"input": 5.0, "output": 15.0})
            usd = (tk["input"] * price["input"]
                   + tk["output"] * price["output"]
                   + tk.get("cache_read", 0) * price.get("cache_read", price["input"])
                   + tk.get("cache_write", 0) * price.get("cache_write", price["input"] * 1.25)
                   ) / 1_000_000
            lines.append(
                f"  {model:30s} ${usd:7.3f}  (in={tk['input']:>9d}t / out={tk['output']:>7d}t / calls={tk['calls']:>4d})"
            )
    # 合計
    total = round(sum(daily_usd.values()), 2)
    lines.append("")
    lines.append(f"## 期間合計: ${total:.2f}")
    return "\n".join(lines)


def main() -> None:
    ap = argparse.ArgumentParser(description="bot events.jsonl の集計 CLI")
    ap.add_argument("--since", default="7d", help="time window: 24h, 7d, 30d, all (default: 7d)")
    ap.add_argument("--component", default=None, help="filter by component name")
    ap.add_argument("--tail", type=int, default=0, help="dump last N raw events")
    ap.add_argument("--failures", action="store_true", help="show only failure events")
    ap.add_argument("--by-user", action="store_true", help="user 別 traffic ranking")
    ap.add_argument("--cost-summary", action="store_true", help="model 別 cost 集計 (★2026-05-23 LEE §4.2)")
    args = ap.parse_args()

    if args.since == "all":
        since_sec = None
    else:
        since_sec = parse_since(args.since)
    events = _read_events(since_sec)

    log_path = _events_log_path()
    print(f"events file: {log_path}")
    print(f"window: since {args.since} ({len(events)} events)")
    if args.component:
        print(f"filter: component={args.component}")
    print()

    if not log_path.exists():
        print("(bot_events.jsonl 未生成 — bot が 1 回も走っていないか、別 BRAIN_ROOT)")
        return

    if args.tail:
        print(_format_tail(events, args.tail))
    elif args.failures:
        print(_format_failures(events, args.component))
    elif args.by_user:
        print(_format_by_user(events))
    elif args.cost_summary:
        print(_format_cost_summary(events))
    else:
        print(_format_summary(events, args.component))


if __name__ == "__main__":
    main()
