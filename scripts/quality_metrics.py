"""scripts/quality_metrics.py — 日次 品質 metric 集計 + 劣化 trend alert

★2026-05-26 海山指示 (C2 + C3): 応答品質の劣化を 24h baseline 比較で検知 → LINE Push。

集計対象:
1. bot_events.jsonl
   - turn_started / turn_finished / turn_failed (= 基本 traffic + fail rate)
   - retrieval_fallback_triggered (= retrieval miss、core/vector 両方失敗時の fallback)
2. clone_improve/response_quality/YYYY-MM-DD.jsonl
   - 3 軸 LLM-as-judge スコア (ai_smell / mirroring_fit / length_appropriate)
   - degraded turn count (= 任意軸 ≦ 2)
3. clone_learning/YYYY-MM.jsonl
   - category=response_quality として LLM auto-discovery が抽出した「品質違反」 件数

出力:
- data/brain/clone_improve/quality_metrics.jsonl (= daily 1 line append、14 日分保持)
- 劣化検知時に LINE Push (= 24h cooldown)

usage:
  python3 scripts/quality_metrics.py             # 今日分 集計 + alert
  python3 scripts/quality_metrics.py --dry-run   # alert 送らない
  python3 scripts/quality_metrics.py --since 7   # 過去 7 日 backfill
"""
from __future__ import annotations

import argparse
import json
import logging
import os
import sys
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Optional

logger = logging.getLogger("quality_metrics")
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")

JST = timezone(timedelta(hours=9))
APP_ROOT = Path(os.getenv("BRAIN_APP_ROOT", "/app"))
BRAIN_ROOT = Path(os.getenv("BRAIN_ROOT", str(APP_ROOT / "data" / "brain")))

# ★2026-07-10 (世界基準評価 #6): bot_events の実出力は BRAIN_ROOT/bot_events/events.jsonl
#   (subdir)。ここが BRAIN_ROOT/bot_events.jsonl (直下 file) を指していたため 31 日間 存在しない
#   file を空振り = n_finished 等が全ゼロで劣化アラートが構造的に発火不能だった。
#   bot_events 側の resolver を単一真実源にして再 drift を根絶 (実解決は call 時 = env 尊重)。
try:
    from bot_events import _events_log_path as _bot_events_path  # type: ignore
except Exception:  # pragma: no cover
    def _bot_events_path() -> Path:
        return BRAIN_ROOT / "bot_events" / "events.jsonl"
RESPONSE_QUALITY_DIR = BRAIN_ROOT / "clone_improve" / "response_quality"
LEARNING_DIR = BRAIN_ROOT / "clone_learning"
METRICS_FILE = BRAIN_ROOT / "clone_improve" / "quality_metrics.jsonl"
ALERT_LOG = BRAIN_ROOT / "quality_metrics_alerts.jsonl"

# alert 閾値 (= env で override 可)
DEGRADATION_THRESHOLD_PCT = float(os.getenv("QUALITY_DEGRADATION_THRESHOLD_PCT", "20"))
MIN_BASELINE_DAYS = int(os.getenv("QUALITY_BASELINE_MIN_DAYS", "3"))  # 最低 3 日無いと比較不可
ALERT_COOLDOWN_HOURS = int(os.getenv("QUALITY_ALERT_COOLDOWN_HOURS", "24"))

sys.path.insert(0, str(Path(__file__).resolve().parent))

try:
    from clone_improve_lib import line_push  # type: ignore
except Exception as e:
    logger.warning(f"clone_improve_lib import failed: {e}")
    def line_push(text: str) -> bool:  # type: ignore
        logger.error(f"[LINE PUSH stub] {text}")
        return False


# ─── 集計 helpers ────────────────────────────────────────────
def _iter_jsonl(path: Path):
    if not path.exists():
        return
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            yield json.loads(line)
        except Exception:
            continue


def _parse_ts(s: str) -> Optional[datetime]:
    try:
        return datetime.fromisoformat(s)
    except Exception:
        return None


def aggregate_bot_events(target_date: datetime.date) -> dict:
    """bot_events.jsonl を target_date (= JST) で filter + 集計."""
    n_started = 0
    n_finished = 0
    n_failed = 0
    n_fallback = 0
    failure_components: dict[str, int] = defaultdict(int)

    for r in _iter_jsonl(_bot_events_path()):
        ts = _parse_ts(r.get("ts", ""))
        if not ts:
            continue
        ts_jst = ts.astimezone(JST) if ts.tzinfo else ts
        if ts_jst.date() != target_date:
            continue
        ev = r.get("event", "")
        if ev == "turn_started":
            n_started += 1
        elif ev == "turn_finished":
            n_finished += 1
        elif ev == "turn_failed":
            n_failed += 1
            comp = r.get("component", "?")
            failure_components[comp] += 1
        elif ev == "retrieval_fallback_triggered":
            n_fallback += 1

    fail_rate = (n_failed / max(n_started, 1)) * 100 if n_started else 0
    fallback_rate = (n_fallback / max(n_finished, 1)) * 100 if n_finished else 0
    return {
        "n_started": n_started,
        "n_finished": n_finished,
        "n_failed": n_failed,
        "n_retrieval_fallback": n_fallback,
        "fail_rate_pct": round(fail_rate, 2),
        "fallback_rate_pct": round(fallback_rate, 2),
        "top_failure_components": dict(sorted(failure_components.items(), key=lambda x: -x[1])[:5]),
    }


def aggregate_response_quality_judge(target_date: datetime.date) -> dict:
    """clone_improve/response_quality/YYYY-MM-DD.jsonl から 3 軸 集計."""
    f = RESPONSE_QUALITY_DIR / f"{target_date.isoformat()}.jsonl"
    if not f.exists():
        return {"n_judged": 0, "available": False}
    scores_ai = []
    scores_mir = []
    scores_len = []
    degraded_turns = 0
    for r in _iter_jsonl(f):
        # ★2026-07-10 (世界基準評価 #6): response_quality の record は 3 軸を r["judge"] に
        #   nest する (clone_response_quality_judge.py の record 形)。top-level を読んでいたため
        #   全て None = n_judged 恒常 0 だった。judge 配下を読む (旧 flat record も後方互換で拾う)。
        j = r.get("judge") if isinstance(r.get("judge"), dict) else r
        ai = j.get("ai_smell")
        mir = j.get("mirroring_fit")
        ln = j.get("length_appropriate")
        try:
            if ai is not None:
                scores_ai.append(float(ai))
            if mir is not None:
                scores_mir.append(float(mir))
            if ln is not None:
                scores_len.append(float(ln))
            # degraded = 任意軸 ≦ 2
            if any(float(v) <= 2 for v in (ai, mir, ln) if v is not None):
                degraded_turns += 1
        except Exception:
            continue
    n = max(len(scores_ai), len(scores_mir), len(scores_len))
    if n == 0:
        return {"n_judged": 0, "available": False}
    return {
        "n_judged": n,
        "available": True,
        "mean_ai_smell": round(sum(scores_ai) / max(len(scores_ai), 1), 2) if scores_ai else None,
        "mean_mirroring_fit": round(sum(scores_mir) / max(len(scores_mir), 1), 2) if scores_mir else None,
        "mean_length_appropriate": round(sum(scores_len) / max(len(scores_len), 1), 2) if scores_len else None,
        "n_degraded": degraded_turns,
        "degraded_rate_pct": round((degraded_turns / n) * 100, 2),
    }


def aggregate_learning_discoveries(target_date: datetime.date) -> dict:
    """clone_learning/YYYY-MM.jsonl で target_date に作成された response_quality 件数."""
    ym = target_date.strftime("%Y-%m")
    f = LEARNING_DIR / f"{ym}.jsonl"
    if not f.exists():
        return {"n_response_quality": 0, "n_total_discoveries": 0}
    n_rq = 0
    n_total = 0
    for r in _iter_jsonl(f):
        ts = _parse_ts(r.get("timestamp", ""))
        if not ts:
            continue
        ts_jst = ts.astimezone(JST) if ts.tzinfo else ts
        if ts_jst.date() != target_date:
            continue
        n_total += 1
        if r.get("category") == "response_quality":
            n_rq += 1
    return {"n_response_quality": n_rq, "n_total_discoveries": n_total}


def collect_daily_metrics(target_date: datetime.date) -> dict:
    """1 日分の全 metric を 1 dict にまとめる."""
    return {
        "date": target_date.isoformat(),
        "generated_at": datetime.now(JST).isoformat(timespec="seconds"),
        "turn": aggregate_bot_events(target_date),
        "quality_judge": aggregate_response_quality_judge(target_date),
        "auto_discovery": aggregate_learning_discoveries(target_date),
    }


# ─── 過去 metrics 読込 + 累積保存 ─────────────────────────────
def load_recent_metrics(days: int = 14) -> list[dict]:
    """quality_metrics.jsonl の直近 days 件を返す (新→旧)."""
    if not METRICS_FILE.exists():
        return []
    items = list(_iter_jsonl(METRICS_FILE))
    by_date: dict[str, dict] = {}
    for m in items:
        d = m.get("date", "")
        if d:
            by_date[d] = m  # 後勝ち = 同日の最新値
    sorted_items = sorted(by_date.values(), key=lambda m: m["date"], reverse=True)
    return sorted_items[:days]


def append_metrics(metrics: dict) -> None:
    METRICS_FILE.parent.mkdir(parents=True, exist_ok=True)
    with METRICS_FILE.open("a", encoding="utf-8") as f:
        f.write(json.dumps(metrics, ensure_ascii=False) + "\n")


# ─── 劣化 trend 検出 ──────────────────────────────────────────
def compute_baseline(history: list[dict], today_date: str) -> dict | None:
    """today 以外の直近 7 日 (= 最低 3 日) で baseline 計算."""
    past = [m for m in history if m.get("date") != today_date][:7]
    if len(past) < MIN_BASELINE_DAYS:
        return None
    # baseline = 過去 7 日平均
    def _mean(vals):
        vals = [v for v in vals if v is not None]
        return sum(vals) / len(vals) if vals else None

    return {
        "n_days_baseline": len(past),
        "mean_fail_rate_pct": _mean([m.get("turn", {}).get("fail_rate_pct", 0) for m in past]),
        "mean_fallback_rate_pct": _mean([m.get("turn", {}).get("fallback_rate_pct", 0) for m in past]),
        "mean_ai_smell": _mean([m.get("quality_judge", {}).get("mean_ai_smell") for m in past]),
        "mean_mirroring_fit": _mean([m.get("quality_judge", {}).get("mean_mirroring_fit") for m in past]),
        "mean_length_appropriate": _mean([m.get("quality_judge", {}).get("mean_length_appropriate") for m in past]),
        "mean_degraded_rate_pct": _mean([m.get("quality_judge", {}).get("degraded_rate_pct", 0) for m in past]),
        "mean_n_response_quality": _mean([m.get("auto_discovery", {}).get("n_response_quality", 0) for m in past]),
    }


def detect_degradation(today: dict, baseline: dict) -> list[dict]:
    """今日 vs baseline、各指標 で degradation 判定. Returns: list of degraded axes."""
    degraded = []

    def _check_worse_high(name: str, label: str, today_val, base_val,
                          threshold_pct: float = DEGRADATION_THRESHOLD_PCT):
        """high = bad な指標 (= fail_rate, fallback_rate, degraded_rate, n_response_quality).
        today > baseline * (1 + threshold/100) で degraded.
        絶対 +1 件以上の差も必要 (= 0→1 で +inf% にならないよう)。
        """
        if today_val is None or base_val is None:
            return
        if base_val == 0:
            if today_val >= 2:  # 0 → 2+ で degraded
                degraded.append({"axis": name, "label": label, "today": today_val,
                                  "baseline": 0, "delta_pct": float("inf"), "kind": "high"})
            return
        delta_pct = ((today_val - base_val) / base_val) * 100
        if delta_pct > threshold_pct and (today_val - base_val) >= 0.5:
            degraded.append({"axis": name, "label": label, "today": today_val,
                              "baseline": round(base_val, 2),
                              "delta_pct": round(delta_pct, 1), "kind": "high"})

    def _check_worse_low(name: str, label: str, today_val, base_val,
                         threshold_pct: float = DEGRADATION_THRESHOLD_PCT / 2):  # 半分 (= 軸 score は変動小)
        """low = bad な指標 (= ai_smell mean, mirroring_fit mean, length_appropriate mean).
        today < baseline * (1 - threshold/100) で degraded.
        """
        if today_val is None or base_val is None:
            return
        if base_val == 0:
            return
        delta_pct = ((today_val - base_val) / base_val) * 100
        if delta_pct < -threshold_pct:
            degraded.append({"axis": name, "label": label, "today": today_val,
                              "baseline": round(base_val, 2),
                              "delta_pct": round(delta_pct, 1), "kind": "low"})

    t = today
    _check_worse_high("fail_rate_pct", "turn fail rate %",
                       t.get("turn", {}).get("fail_rate_pct"),
                       baseline.get("mean_fail_rate_pct"))
    _check_worse_high("fallback_rate_pct", "retrieval fallback rate %",
                       t.get("turn", {}).get("fallback_rate_pct"),
                       baseline.get("mean_fallback_rate_pct"))
    _check_worse_high("degraded_rate_pct", "応答品質 degraded rate %",
                       t.get("quality_judge", {}).get("degraded_rate_pct"),
                       baseline.get("mean_degraded_rate_pct"))
    _check_worse_high("n_response_quality", "auto-discovered response_quality 件",
                       t.get("auto_discovery", {}).get("n_response_quality"),
                       baseline.get("mean_n_response_quality"))
    _check_worse_low("mean_ai_smell", "ai_smell mean (5=最良)",
                      t.get("quality_judge", {}).get("mean_ai_smell"),
                      baseline.get("mean_ai_smell"))
    _check_worse_low("mean_mirroring_fit", "mirroring_fit mean (5=最良)",
                      t.get("quality_judge", {}).get("mean_mirroring_fit"),
                      baseline.get("mean_mirroring_fit"))
    _check_worse_low("mean_length_appropriate", "length_appropriate mean (5=最良)",
                      t.get("quality_judge", {}).get("mean_length_appropriate"),
                      baseline.get("mean_length_appropriate"))
    return degraded


# ─── alert ────────────────────────────────────────────────────
def _alert_seen_recently(cooldown_hours: int = ALERT_COOLDOWN_HOURS) -> bool:
    if not ALERT_LOG.exists():
        return False
    cutoff = datetime.now(JST) - timedelta(hours=cooldown_hours)
    try:
        for line in reversed(ALERT_LOG.read_text(encoding="utf-8").splitlines()[-50:]):
            try:
                r = json.loads(line)
                ts = _parse_ts(r.get("ts", ""))
                if ts and ts > cutoff:
                    return True
            except Exception:
                continue
    except Exception:
        pass
    return False


def _log_alert(severity: str, summary: str, degraded: list[dict]) -> None:
    ALERT_LOG.parent.mkdir(parents=True, exist_ok=True)
    with ALERT_LOG.open("a", encoding="utf-8") as f:
        f.write(json.dumps({
            "ts": datetime.now(JST).isoformat(timespec="seconds"),
            "severity": severity,
            "summary": summary,
            "degraded": degraded,
        }, ensure_ascii=False) + "\n")


def build_alert_text(today: dict, baseline: dict, degraded: list[dict]) -> str:
    """LINE Push 用 text を組み立て."""
    lines = [
        f"⚠️ [Personal Brain] 応答品質 trend 警告 ({today.get('date', '?')})",
        f"baseline ({baseline.get('n_days_baseline', 0)} 日平均) 比 {DEGRADATION_THRESHOLD_PCT:.0f}% 以上 悪化軸:",
        "",
    ]
    for d in degraded:
        arrow = "↑" if d["kind"] == "high" else "↓"
        delta = d["delta_pct"]
        delta_str = f"+{delta:.0f}%" if delta > 0 else f"{delta:.0f}%"
        lines.append(f"  {arrow} {d['label']}: {d['today']} (基準 {d['baseline']}) {delta_str}")
    lines.append("")
    lines.append(f"詳細: /admin/review/quality?token=...")
    lines.append(f"発見 review: /admin/review/learning?token=...")
    return "\n".join(lines)


def run_once(target_date: Optional[datetime.date] = None, dry_run: bool = False) -> int:
    """1 日分 集計 + alert (= cron daily 04:05)."""
    today_date = target_date or (datetime.now(JST) - timedelta(days=1)).date()
    logger.info(f"collecting quality metrics for {today_date}")

    metrics = collect_daily_metrics(today_date)
    if not dry_run:
        append_metrics(metrics)
    logger.info(f"metrics: {json.dumps(metrics, ensure_ascii=False, default=str)}")

    # ★2026-07-10 (世界基準評価 #6): zero-data guard。events file が存在するのに集計が
    #   全ゼロ = path/schema drift で「劣化アラートが構造的に沈黙」した 31 日事故の再発検知。
    #   events が実在 (非空) かつ n_finished==0 の時だけ loud_fail (真の無 traffic 日は file が
    #   薄い/無いので誤発火しない)。target が過去 backfill の時は skip。
    if not dry_run and target_date is None:
        try:
            ev_path = _bot_events_path()
            ev_nonempty = ev_path.exists() and ev_path.stat().st_size > 0
            n_fin = int((metrics.get("events") or {}).get("n_finished", 0))
            from clone_improve_lib import loud_fail  # type: ignore
            loud_fail(
                "quality_metrics_pipeline",
                not (ev_nonempty and n_fin == 0),
                f"bot_events は存在 ({ev_path}) だが n_finished==0 = 集計 path/schema drift の疑い "
                f"(劣化アラートが沈黙する)",
                threshold=2, cooldown_h=48,
            )
        except Exception as ge:
            logger.warning(f"zero-data guard failed: {ge}")

    # baseline 計算 + degradation 検出
    history = load_recent_metrics(days=14)
    baseline = compute_baseline(history, today_date.isoformat())
    if not baseline:
        logger.info(f"baseline 不十分 (= 過去 {MIN_BASELINE_DAYS} 日未満)、alert skip")
        return 0

    degraded = detect_degradation(metrics, baseline)
    if not degraded:
        logger.info("no degradation, healthy")
        return 0

    summary = f"{len(degraded)} 軸 degraded"
    logger.warning(f"degradation detected: {summary}")
    if _alert_seen_recently():
        logger.info("alert cooldown active, skip Push")
        return 0

    text = build_alert_text(metrics, baseline, degraded)
    if dry_run:
        print("=== ALERT (dry-run) ===")
        print(text)
        return 1

    sent = line_push(text)
    _log_alert("warning" if sent else "skip", summary, degraded)
    return 1 if sent else 0


def main():
    ap = argparse.ArgumentParser(description="品質 metric 集計 + 劣化 alert (★2026-05-26 海山 C2+C3)")
    ap.add_argument("--dry-run", action="store_true", help="LINE Push せず、stdout のみ")
    ap.add_argument("--date", type=str, help="YYYY-MM-DD、default = 昨日 JST")
    ap.add_argument("--since", type=int, default=None,
                     help="過去 N 日 backfill (= 各日 1 line 追加、alert は最新日のみ)")
    args = ap.parse_args()

    target = None
    if args.date:
        try:
            target = datetime.fromisoformat(args.date).date()
        except ValueError:
            print(f"invalid date: {args.date}", file=sys.stderr)
            sys.exit(2)

    if args.since:
        # backfill モード
        today = (datetime.now(JST) - timedelta(days=1)).date()
        for n in range(args.since, 0, -1):
            d = today - timedelta(days=n - 1)
            run_once(target_date=d, dry_run=args.dry_run)
        return

    n_alerts = run_once(target_date=target, dry_run=args.dry_run)
    sys.exit(0 if n_alerts == 0 else 1)


if __name__ == "__main__":
    main()
