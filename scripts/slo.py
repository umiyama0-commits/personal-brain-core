#!/usr/bin/env python3
"""SLO / error budget の定義と実測 (★2026-07-10 世界基準評価 S2)。

背景: 監視は event 駆動 alert のみで、可用性・応答成功率の**目標値と実測値**が無かった。
結果として gate block化・alert 閾値・復旧投資の判断が毎回主観になっていた。
世界基準の SRE は「監視がある」でなく「目標に対する残余 budget で行動が変わる」。

SLO 2 本 (単機ホームラボで現実的な最小構成):
- SLO-1 turn 成功率 ≥ 99% / 週  — bot_events (turn_finished/turn_failed) から決定論算出。
- SLO-2 外形 /health 可用性 ≥ 99.5% / 月 — 外部監視 (UptimeRobot) の実測が要る。本 module は
  目標を宣言し、SLO_AVAILABILITY_PCT env (監視側から流し込む) があれば実測、無ければ「未接続」。

出力は clone_weekly_report が決定論で append する (LLM に数字を語らせない = 捏造禁止)。
error budget consumed = 実失敗率 / (1 - 目標)。100% 超 = SLO 割れ = 復旧投資の判断トリガー。
"""
from __future__ import annotations

import os
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

JST = timezone(timedelta(hours=9))

# ─── SLO 目標 (単一真実源) ───
SLO_TURN_SUCCESS_TARGET = 0.99      # turn 成功率 ≥ 99% / 週
SLO_AVAILABILITY_TARGET = 0.995     # 外形 /health 可用性 ≥ 99.5% / 月


def _bot_events_path() -> Path:
    try:
        from bot_events import _events_log_path  # type: ignore
        return _events_log_path()
    except Exception:
        root = os.getenv("BRAIN_ROOT") or (
            (os.getenv("BRAIN_APP_ROOT") or "/app") + "/data/brain")
        return Path(root) / "bot_events" / "events.jsonl"


def _turn_counts(days: int = 7) -> tuple[int, int]:
    """直近 days 日の (turn_finished, turn_failed) を返す。"""
    import json
    p = _bot_events_path()
    if not p.exists():
        return (0, 0)
    cutoff = datetime.now(JST) - timedelta(days=days)
    fin = fail = 0
    for line in p.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        try:
            r = json.loads(line)
        except Exception:
            continue
        ts = r.get("ts", "")
        try:
            t = datetime.fromisoformat(ts.replace("Z", "+00:00"))
        except Exception:
            continue
        t = t.astimezone(JST) if t.tzinfo else t.replace(tzinfo=JST)
        if t < cutoff:
            continue
        ev = r.get("event", "")
        if ev == "turn_finished":
            fin += 1
        elif ev == "turn_failed":
            fail += 1
    return (fin, fail)


def compute_slo(days: int = 7) -> dict:
    fin, fail = _turn_counts(days)
    total = fin + fail
    success_rate = (fin / total) if total else None
    fail_rate = (fail / total) if total else 0.0
    # error budget consumed = 実失敗率 / 許容失敗率 (1 - target)
    budget = (1 - SLO_TURN_SUCCESS_TARGET)
    consumed = (fail_rate / budget) if budget > 0 else 0.0

    # 外形可用性: 監視側 (UptimeRobot 等) が env で流し込むまで未接続
    avail_env = os.getenv("SLO_AVAILABILITY_PCT", "").strip()
    availability = None
    try:
        availability = float(avail_env) / 100.0 if avail_env else None
    except Exception:
        availability = None

    return {
        "window_days": days,
        "turn_total": total,
        "turn_success_rate": success_rate,
        "turn_success_target": SLO_TURN_SUCCESS_TARGET,
        "turn_error_budget_consumed": consumed,   # 1.0 超 = SLO 割れ
        "turn_slo_met": (success_rate is None) or (success_rate >= SLO_TURN_SUCCESS_TARGET),
        "availability": availability,
        "availability_target": SLO_AVAILABILITY_TARGET,
        "availability_connected": availability is not None,
    }


def build_slo_block(days: int = 7) -> str:
    """週次レポート末尾に決定論で append する SLO ブロック。"""
    s = compute_slo(days)
    lines = ["## SLO / エラーバジェット (決定論算出)"]
    if s["turn_total"] == 0:
        lines.append("- turn 成功率: 対象 turn 0 件 (計測データ薄)")
    else:
        sr = s["turn_success_rate"] * 100
        mark = "✅" if s["turn_slo_met"] else "🔴"
        lines.append(
            f"- {mark} turn 成功率: {sr:.2f}% (目標 ≥99%、直近{days}日 {s['turn_total']}turn) "
            f"／ error budget 消費 {s['turn_error_budget_consumed']*100:.0f}%"
        )
    if s["availability_connected"]:
        av = s["availability"] * 100
        mark = "✅" if av >= SLO_AVAILABILITY_TARGET * 100 else "🔴"
        lines.append(f"- {mark} 外形/health 可用性: {av:.2f}% (目標 ≥99.5%)")
    else:
        lines.append("- 外形/health 可用性: 目標 ≥99.5%／月 (実測は UptimeRobot 接続後。SLO_AVAILABILITY_PCT env で流し込み)")
    lines.append("_error budget 100% 超 = SLO 割れ = 復旧投資/gate 判断のトリガー (主観でなく残余で動く)_")
    return "\n".join(lines)


if __name__ == "__main__":
    print(build_slo_block())
