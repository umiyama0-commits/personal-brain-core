"""usage_analytics.py — Personal Brain bot usage 集計 (★2026-05-24 Feature 2/4).

# 役割

bot_events.jsonl から ROI 計測用の usage metric を集計:
- 期間別 query 数 (= 月間 1,000 → 10,000 への path tracking)
- DM vs group split (= Tier 0 後の channel 別利用)
- user 別 (= heavy user 識別、heavy 100 × 20/月 target)
- failure rate / latency (= 品質指標)
- silent listen 比率 (= group 内 mention 率)
- Cohere rerank usage (= cost tracking、$2/1000 searches)
- Drive 利用 (= 共有 URL fetch 回数 / permission_denied 率)

# 設計

- iter_events() を時間窓ごとに呼ぶ
- JSON 構造で返す (= /api/admin/usage が dict 返却、/admin/usage HTML が render)
- pure aggregation (= LLM call なし、< 100ms target)
- 既存 bot_metrics.py CLI と相補 (= CLI = local debug、本 module = Web UI / programmatic)

# 想定 ROI tracking

Phase 1 target = 月 1,000 query / $1 per query = $1,000/月
Heavy user pattern = 100 user × 20 query/月

dashboard で見えるべき:
- 今月までの累計 query (= 進捗 vs target)
- 過去 7d/30d trend (= 増加 / 減少)
- top user (= heavy 形成中の核)
- DM vs group (= channel diversification)
- failure rate (= 信頼性)
"""
from __future__ import annotations

import logging
import os
import sys
from collections import defaultdict
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

# bot_events を import (= scripts/ from app root)
_REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_REPO / "scripts"))

JST = timezone(timedelta(hours=9))


def _percentile(values: list[float], pct: float) -> float:
    if not values:
        return 0.0
    s = sorted(values)
    k = max(0, min(len(s) - 1, int(round(pct / 100 * (len(s) - 1)))))
    return s[k]


def _events_in_window(since_sec: int) -> list[dict]:
    """指定窓内の events を取得。"""
    try:
        from bot_events import iter_events  # type: ignore
    except Exception as e:
        logger.warning(f"bot_events import failed: {e}")
        return []
    return list(iter_events(since_sec=since_sec))


# ★2026-06-09 海山指示「バッチ等はアクセスとしてカウントしない」: video-align batch / eval /
# synthetic / health / hybrid 等の自動化呼び出しを実ユーザー利用 (queries) から除外する。
# 実ユーザーの LINE Works user_id は hex UUID (= 0-9a-f) なので、下記 named prefix とは衝突しない
# (video/eval/test/health/hybrid/synthetic は非 hex 文字を含む → 誤除外なし)。空 user_id も
# 内部/eval 呼び出しなので除外。
_AUTOMATED_UID_PREFIXES = (
    "video_align", "video_al",  # 動画アラインメント batch (main.py video_align_local)
    "synthetic",                 # synthetic employee
    "health", "hybrid",          # health check / hybrid 検索 eval
    "eval", "test", "regression",  # eval runner / テスト / 夜間 regression
)


def _is_automated_user(uid: str) -> bool:
    """自動化/batch/eval の user_id か (= 実ユーザー利用に数えない)。空も自動化扱い。"""
    if not uid:
        return True
    u = uid.lower()
    return any(u.startswith(p) for p in _AUTOMATED_UID_PREFIXES)


def aggregate_usage(since_sec: int = 86400 * 30) -> dict:
    """期間内の usage を集計、ROI tracking 用 dict 返却.

    Args:
        since_sec: 集計対象期間 (秒、default 30 日 = 月次集計)

    Returns:
        {
          "window_sec": int, "window_label": str,
          "summary": {
            "total_queries": int,        # ROI denominator
            "total_replies": int,        # = bot 応答数
            "failure_rate_pct": float,
            "avg_latency_ms": int,
            "p95_latency_ms": int,
          },
          "channel_split": {
            "dm_count": int, "group_count": int, "silent_listen_count": int,
          },
          "top_users": [{"user_id": str, "turns": int, "fail": int}],
          "components": {
            "clone_respond": {"ok": N, "fail": N, "avg_ms": int},
            "cohere_rerank": {"ok": N, "fail": N},
            "drive_ingest": {"ok": N, "fail": N},
            ...
          },
          "daily_trend": [
            {"date": "YYYY-MM-DD", "queries": int, "failures": int},
            ...
          ],
          "roi_progress": {
            "monthly_target": 1000,
            "current_pace_estimate": int,   # 30d 換算
            "progress_pct": float,
          },
        }
    """
    events = _events_in_window(since_sec)

    # 基本 counter
    n_turn_started = 0
    n_turn_finished = 0
    n_turn_failed = 0
    latencies: list[float] = []

    # channel split
    dm_count = 0
    group_count = 0
    silent_listen_count = 0

    # user breakdown
    by_user: dict[str, dict] = defaultdict(lambda: {"turns": 0, "fail": 0, "latencies": []})

    # component breakdown
    by_comp: dict[str, dict] = defaultdict(lambda: {"ok": 0, "fail": 0, "latencies": []})

    # daily trend (queries=実ユーザー / automated=batch・eval 等、★海山指示で分離)
    by_date: dict[str, dict] = defaultdict(lambda: {"queries": 0, "failures": 0, "automated": 0})

    for e in events:
        comp = e.get("component", "unknown")
        ev = e.get("event")
        uid = (e.get("user_id") or "")
        is_auto = _is_automated_user(uid)  # ★batch/eval/synthetic は実利用に数えない
        ts_str = e.get("ts", "")
        try:
            date_str = ts_str[:10] if ts_str else ""
        except Exception:
            date_str = ""

        # component breakdown (= 診断用なので自動化も含め全部記録)
        if ev == "turn_started":
            if comp == "clone_respond" and not is_auto:
                n_turn_started += 1
        elif ev == "turn_finished":
            if comp == "clone_respond" and not is_auto:
                n_turn_finished += 1
            by_comp[comp]["ok"] += 1
            lat = e.get("elapsed_ms")
            if lat is not None:
                try:
                    lat_f = float(lat)
                    by_comp[comp]["latencies"].append(lat_f)
                    if comp == "clone_respond" and not is_auto:
                        latencies.append(lat_f)
                        if uid:
                            by_user[uid]["latencies"].append(lat_f)
                except Exception:
                    pass
            if uid and comp == "clone_respond" and not is_auto:
                by_user[uid]["turns"] += 1
            if date_str and comp == "clone_respond":
                if is_auto:
                    by_date[date_str]["automated"] += 1
                else:
                    by_date[date_str]["queries"] += 1
        elif ev == "turn_failed":
            if comp == "clone_respond" and not is_auto:
                n_turn_failed += 1
            by_comp[comp]["fail"] += 1
            if uid and comp == "clone_respond" and not is_auto:
                by_user[uid]["fail"] += 1
            if date_str and comp == "clone_respond" and not is_auto:
                by_date[date_str]["failures"] += 1

        # channel split (= group webhook の silent listen は別 event)
        # main.py の _handle_lineworks_group_message で channel_id ありの clone_respond turn は group
        # ただし bot_events に channel_id 直接記録してないので、推定: clone_respond で channel_id field あれば group
        ch = e.get("channel_id")
        if ev == "turn_finished" and comp == "clone_respond":
            if ch:
                group_count += 1
            else:
                dm_count += 1

    # silent listen count は専用 event を future に追加した時に拾う (= 現状 0、後続 PR で main.py 拡張)
    # 現状の bot_events では silent listen を明示 event 化してないので推定不可、0 で返す

    # summary
    failure_rate_pct = round((n_turn_failed / n_turn_started * 100) if n_turn_started else 0, 1)
    avg_latency_ms = int(sum(latencies) / len(latencies)) if latencies else 0
    p95_latency_ms = int(_percentile(latencies, 95))

    # top users
    top_users = sorted(by_user.items(), key=lambda kv: -kv[1]["turns"])[:20]
    top_users_list = [
        {
            "user_id": uid[:16],
            "turns": d["turns"],
            "fail": d["fail"],
            "avg_ms": int(sum(d["latencies"]) / len(d["latencies"])) if d["latencies"] else 0,
        }
        for uid, d in top_users
    ]

    # component summary
    components = {
        comp: {
            "ok": d["ok"],
            "fail": d["fail"],
            "avg_ms": int(sum(d["latencies"]) / len(d["latencies"])) if d["latencies"] else 0,
        }
        for comp, d in by_comp.items()
    }

    # daily trend (= last 30 days)
    daily_trend = sorted(
        [{"date": d, "queries": v["queries"], "failures": v["failures"],
          "automated": v.get("automated", 0)} for d, v in by_date.items()],
        key=lambda x: x["date"],
    )

    # ROI progress
    n_days_in_window = max(1, since_sec // 86400)
    queries_per_day = n_turn_started / n_days_in_window
    monthly_pace = int(queries_per_day * 30)
    progress_pct = round((monthly_pace / 1000 * 100), 1)

    return {
        "window_sec": since_sec,
        "window_label": _format_window_label(since_sec),
        "generated_at": datetime.now(JST).isoformat(timespec="seconds"),
        "summary": {
            "total_queries": n_turn_started,
            "total_replies": n_turn_finished,
            "total_failures": n_turn_failed,
            "failure_rate_pct": failure_rate_pct,
            "avg_latency_ms": avg_latency_ms,
            "p95_latency_ms": p95_latency_ms,
        },
        "channel_split": {
            "dm_count": dm_count,
            "group_count": group_count,
            "silent_listen_count": silent_listen_count,
            "group_pct": round((group_count / (dm_count + group_count) * 100) if (dm_count + group_count) else 0, 1),
        },
        "top_users": top_users_list,
        "components": components,
        "daily_trend": daily_trend,
        "roi_progress": {
            "monthly_target": 1000,
            "current_pace_estimate_monthly": monthly_pace,
            "progress_pct": progress_pct,
            "phase_1_full_target": 10000,
            "queries_per_day_avg": round(queries_per_day, 1),
        },
    }


def _format_window_label(since_sec: int) -> str:
    if since_sec >= 86400 * 7:
        return f"{since_sec // 86400} days"
    if since_sec >= 3600:
        return f"{since_sec // 3600} hours"
    if since_sec >= 60:
        return f"{since_sec // 60} minutes"
    return f"{since_sec} seconds"


# ─── ★2026-05-29 海山指示「各種 API 料金 + 課金状況の track 機能」: cost aggregation ──
# bot_events.jsonl の turn_finished.usage (= COST_TRACKING_ENABLED で記録) から
# model 別 / 日別 / component 別 / provider 別 の USD を概算する。
#
# 重要な scope 注記:
#   - usage field は主に clone_respond turn に記録される (= bot 応答の実コスト)。
#   - 裏 task (sleep_time_agent / memory 更新) や cron (regression / quality-judge 等)
#     は bot_run_context を通らないため、本集計は「下限推定」。真の総額は LiteLLM /spend。
#   - dashboard は 本推定 (per-turn) + LiteLLM /spend (確定値) の両建てで見せる。
#
# PRICE_TABLE は public 価格の目安 (USD per 1M token、★2026-05-29 時点、要メンテ)。
# cache_read = prompt cache hit 時の input 単価、cache_write = cache 生成時単価。
COST_PRICE_TABLE: dict[str, dict[str, float]] = {
    # Anthropic — ★2026-05-29 修正: Opus 4.7/4.8 は $5/$25/$0.5/$6.25 (公式 pricing 確認済、
    #   platform.claude.com/docs pricing)。旧値 $15/$75/$1.5/$18.75 は Opus 4.1 の単価で、
    #   4-7 に誤適用し全 Opus call を 3x 過大計上していた。
    #   claude-opus-4-20250514 (= 旧 Opus 4 2025-05) は本当に $15/$75 系なので据置 (公式 deprecated 行と一致)。
    "claude-opus-4-8":        {"input": 5.0,  "output": 25.0, "cache_read": 0.5,  "cache_write": 6.25},
    "claude-opus-4-7":        {"input": 5.0,  "output": 25.0, "cache_read": 0.5,  "cache_write": 6.25},
    "claude-opus-4-20250514": {"input": 15.0, "output": 75.0, "cache_read": 1.5,  "cache_write": 18.75},
    "claude-haiku-4-5":       {"input": 1.0,  "output": 5.0,  "cache_read": 0.1,  "cache_write": 1.25},
    # OpenAI
    # ★2026-07-31 公式 pricing と突合して是正 (developers.openai.com/api/docs/pricing)。
    #   gpt-5.4 の旧値 $10/$40/$2.5 は **input 4倍・output 2.7倍・cache_read 10倍**の過大計上で、
    #   コスト表示が実額の約4.5倍に膨らんでいた (bot_metrics 側は 677598c で是正済、こちらが積み残し)。
    #   cache_write=0: OpenAI はキャッシュ**書込を課金しない** (Anthropic のみ)。明示しないと
    #   下流の fallback が Anthropic 式 1.25x を当ててしまう。
    "gpt-4o":                 {"input": 2.5,  "output": 10.0,  "cache_read": 1.25, "cache_write": 0.0},
    "gpt-5.4":                {"input": 2.5,  "output": 15.0,  "cache_read": 0.25, "cache_write": 0.0},
    "gpt-5.4-pro":            {"input": 30.0, "output": 120.0, "cache_write": 0.0},
    "gpt-5.4-mini":           {"input": 0.75, "output": 4.5,   "cache_read": 0.075, "cache_write": 0.0},
    # ★2026-07-31 GPT-5.6 世代 (OpenAI 大幅値下げ)。A/B 中の luna を **未知モデル扱いの
    #   fallback ($5/$15) で 25 倍に過大計上しない**ために必須 (無いと A/B が逆の結論を出す)。
    "gpt-5.6-luna":           {"input": 0.20, "output": 1.20,  "cache_read": 0.02, "cache_write": 0.0},
    "gpt-5.6-terra":          {"input": 2.0,  "output": 12.0,  "cache_read": 0.20, "cache_write": 0.0},
    "gpt-5.6-sol":            {"input": 5.0,  "output": 30.0,  "cache_read": 0.50, "cache_write": 0.0},
    "gpt-5-pro":              {"input": 60.0, "output": 240.0},
    "gpt-5-codex":            {"input": 10.0, "output": 40.0},
    "text-embedding-3-small": {"input": 0.02, "output": 0.0},
    "whisper-1":              {"input": 0.0,  "output": 0.0},  # per-minute 課金、token 集計外
}
_COST_FALLBACK_PRICE = {"input": 5.0, "output": 15.0}

# LiteLLM alias (= litellm_config.yaml の model_name) → 課金 model 名 (PRICE_TABLE key)。
# events.jsonl の ctx["model"] は LiteLLM response の model field (= dated 変種 or alias)
# のため、確実に price を引けるよう解決する。
COST_MODEL_ALIASES: dict[str, str] = {
    "smart": "claude-opus-4-8",
    "smart-legacy": "claude-opus-4-20250514",
    "smart-fallback": "gpt-4o",
    "contextualize": "claude-haiku-4-5",
    "fast": "gpt-4o",
    "default": "gpt-4o",
    "smart-gpt": "gpt-5.4",
    "smart-gpt-pro": "gpt-5.4-pro",
    "fast-gpt": "gpt-5.4-mini",
    # ★2026-07-31 GPT-5.6 世代 (A/B 中)。alias 解決が無いと未知モデル扱いになり
    #   fallback $5/$15 = luna 実単価の 25 倍で計上され、A/B が逆の結論を出す。
    "smart-luna": "gpt-5.6-luna",
    "smart-terra": "gpt-5.6-terra",
    "code": "gpt-5.4-pro",
    "code-max": "gpt-5-pro",
    "whisper": "whisper-1",
}
# prefix match 用: 長い key を優先 (= 'gpt-5.4-mini' を 'gpt-5.4' より先に判定)
_COST_PRICE_KEYS_BY_LEN = sorted(COST_PRICE_TABLE.keys(), key=len, reverse=True)


def _cost_model_short(model: str) -> str:
    """litellm proxy 形式 'anthropic/claude-opus-4-7' → 'claude-opus-4-7'."""
    if not model:
        return "?"
    return model.split("/", 1)[1] if "/" in model else model


def _cost_canonical(model: str) -> str:
    """raw model 文字列 → PRICE_TABLE で引ける canonical 名に解決.

    - 'anthropic/claude-opus-4-7' → 'claude-opus-4-7' (provider prefix 除去)
    - litellm alias 'smart' → 'claude-opus-4-8'
    - dated 変種 'gpt-4o-2024-08-06' → 'gpt-4o' (prefix match)
    解決不能なら cleaned 名をそのまま返す (= fallback price 適用)。
    """
    m = _cost_model_short(model)
    if m == "?":
        return m
    if m in COST_PRICE_TABLE:
        return m
    if m in COST_MODEL_ALIASES:
        return COST_MODEL_ALIASES[m]
    for key in _COST_PRICE_KEYS_BY_LEN:
        if m.startswith(key):
            return key
    return m


def _cost_provider(model: str) -> str:
    """model 名 → 課金先 provider (= 海山の関心軸 Claude vs OpenAI)."""
    m = model.lower()
    if m.startswith(("claude", "anthropic")):
        return "Anthropic (Claude)"
    if m.startswith(("gpt", "o1", "o3", "whisper", "text-embedding", "openai")):
        return "OpenAI"
    if m.startswith(("gemini", "google")):
        return "Google (Gemini)"
    return "other"


def _cost_usd(tk: dict, model: str) -> float:
    """token bucket (single model) → USD。bucket は必ず単一 model 分であること."""
    price = COST_PRICE_TABLE.get(model, _COST_FALLBACK_PRICE)
    base_in = price.get("input", 5.0)
    return (
        tk.get("input", 0) * base_in
        + tk.get("output", 0) * price.get("output", 15.0)
        + tk.get("cache_read", 0) * price.get("cache_read", base_in)
        + tk.get("cache_write", 0) * price.get("cache_write", base_in * 1.25)
    ) / 1_000_000


def aggregate_cost(since_sec: int = 86400 * 14) -> dict:
    """events.jsonl の turn_finished.usage から cost を概算集計.

    Args:
        since_sec: 集計窓 (秒、default 14 日)

    Returns dict:
        {
          "has_usage_data": bool,        # usage 付き turn が 1 件も無ければ False
          "window_label": str, "generated_at": str,
          "totals": {"usd": float, "calls": int, "avg_daily_usd": float,
                     "monthly_projection_usd": float, "n_days": int},
          "today": {"date": str, "usd": float},
          "budget": {"cap_usd": float, "today_pct": float},
          "by_provider": [{"provider": str, "usd": float, "pct": float}],
          "by_model": [{"model","provider","usd","input","output",
                        "cache_read","cache_write","calls","known_price"}],
          "by_component": [{"component","usd","calls"}],
          "daily_trend": [{"date","usd","delta_pct","providers":{...}}],
          "cache": {"anthropic_input_tokens","anthropic_cache_read_tokens",
                    "anthropic_cache_write_tokens","cache_hit_pct"},
        }
    """
    events = _events_in_window(since_sec)

    def _tk() -> dict:
        return {"input": 0, "output": 0, "cache_read": 0, "cache_write": 0, "calls": 0}

    by_date_model: dict[str, dict[str, dict]] = defaultdict(lambda: defaultdict(_tk))
    by_model: dict[str, dict] = defaultdict(_tk)
    by_comp_model: dict[str, dict[str, dict]] = defaultdict(lambda: defaultdict(_tk))
    n_turns_with_usage = 0

    for e in events:
        if e.get("event") != "turn_finished":
            continue
        usage = e.get("usage")
        if not isinstance(usage, dict) or not usage:
            continue
        ts = e.get("ts", "")
        if not ts or len(ts) < 10:
            continue
        date = ts[:10]
        model = _cost_canonical(e.get("model") or "?")
        comp = e.get("component", "unknown")

        # usage token 抽出 — convention-robust 正規化 (★2026-05-29 二重課金 fix)。
        # LiteLLM /v1/chat/completions (= 本番経路) は OpenAI 形式に正規化し、
        #   prompt_tokens = 通常 input + cache_read + cache_write を「合算」した値を返す
        #   (出典: LiteLLM AnthropicConfig.calculate_usage / docs Prompt Caching)。
        # 旧 code は prompt_tokens を input とした上で cache_read/write を別途加算 →
        #   cached token を full price と cache price で二重課金し、かつ cache_hit_pct の
        #   分母 (input+cr+cw) を膨らませ hit 率を過小報告した (真 94% が 48.6% に見える等)。
        # 対策: cache 分を差し引いた「uncached input」のみ input bucket に積む。
        pt = int(usage.get("prompt_tokens") or 0)
        it = int(usage.get("input_tokens") or 0)
        out = int(usage.get("completion_tokens") or usage.get("output_tokens") or 0)
        cr = int(usage.get("cache_read_input_tokens") or 0)
        cw = int(usage.get("cache_creation_input_tokens") or 0)
        # OpenAI は cache read を prompt_tokens_details.cached_tokens に入れる
        ptd = usage.get("prompt_tokens_details") or {}
        if not cr and isinstance(ptd, dict):
            cr = int(ptd.get("cached_tokens") or 0)
        # total input (cache 含む) を両 convention で正規化:
        #   pt あり (OpenAI/LiteLLM): prompt_tokens は既に cache 込み合計。ただし
        #     pt < cr+cw の時は prompt_tokens が cache 抜き = 旧 LiteLLM/Anthropic-native
        #     とみなし pt+cr+cw を total とする (version 差・将来 regression 耐性、算術自動判定)。
        #   pt 無し (Anthropic-native): input_tokens = uncached のみ → it+cr+cw が total。
        if pt:
            total_in = pt if pt >= cr + cw else pt + cr + cw
        else:
            total_in = it + cr + cw
        inp = max(0, total_in - cr - cw)   # uncached input のみ (cache 分は別 bucket で課金)

        for bucket in (by_date_model[date][model], by_model[model], by_comp_model[comp][model]):
            bucket["input"] += inp
            bucket["output"] += out
            bucket["cache_read"] += cr
            bucket["cache_write"] += cw
            bucket["calls"] += 1
        n_turns_with_usage += 1

    if n_turns_with_usage == 0:
        return {
            "has_usage_data": False,
            "window_sec": since_sec,
            "window_label": _format_window_label(since_sec),
            "generated_at": datetime.now(JST).isoformat(timespec="seconds"),
            "note": (
                "events.jsonl に usage field 付き turn_finished が無い。"
                "記録には COST_TRACKING_ENABLED=1 が必要 (default OFF)。"
                "設定後 数日 蓄積で表示される。確定値は LiteLLM /spend (= /api/cost-investigation) を参照。"
            ),
        }

    # 日別 USD + provider split
    # ★2026-07-31: 丸めは**表示用だけ**に留め、集計は未丸めで持つ。旧実装は日次を round(,2) した
    #   合計を window_total にする一方、provider 別は未丸めで積んでいたため、両者の比 (pct) が
    #   100% に揃わなかった (実測 99.4%)。GPT-5.6 luna のように桁が 1/12 になるモデルが混ざると
    #   2 桁丸めの相対誤差が拡大し、A/B のコスト比較を歪めるため恒久修正する。
    daily_usd: dict[str, float] = {}          # 表示用 (丸め)
    daily_usd_raw: dict[str, float] = {}      # 集計用 (未丸め)
    daily_by_provider: dict[str, dict[str, float]] = {}
    for date, models in by_date_model.items():
        tot = 0.0
        prov_usd: dict[str, float] = defaultdict(float)
        for model, tk in models.items():
            u = _cost_usd(tk, model)
            tot += u
            prov_usd[_cost_provider(model)] += u
        daily_usd_raw[date] = tot
        daily_usd[date] = round(tot, 2)
        daily_by_provider[date] = {k: round(v, 2) for k, v in prov_usd.items()}
    dates_sorted = sorted(daily_usd.keys())

    # model 別 (window 合計、USD desc)
    model_rows = []
    for model, tk in by_model.items():
        model_rows.append({
            "model": model,
            "provider": _cost_provider(model),
            "usd": round(_cost_usd(tk, model), 2),
            "input": tk["input"], "output": tk["output"],
            "cache_read": tk["cache_read"], "cache_write": tk["cache_write"],
            "calls": tk["calls"],
            "known_price": model in COST_PRICE_TABLE,
        })
    model_rows.sort(key=lambda r: -r["usd"])

    # component 別 (= どの機能が高コストか、USD desc)
    comp_rows = []
    for comp, models in by_comp_model.items():
        usd = sum(_cost_usd(tk, m) for m, tk in models.items())
        calls = sum(tk["calls"] for tk in models.values())
        comp_rows.append({"component": comp, "usd": round(usd, 2), "calls": calls})
    comp_rows.sort(key=lambda r: -r["usd"])

    # provider 別 (window 合計)
    # ★2026-07-31: provider 集計も **未丸め**で積む (model_rows の表示用丸め値を再利用しない)。
    #   pct の分母も未丸めの window 合計にする = 比率が 100% に揃う。
    prov_usd_total: dict[str, float] = defaultdict(float)
    for model, tk in by_model.items():
        prov_usd_total[_cost_provider(model)] += _cost_usd(tk, model)
    window_total_raw = sum(daily_usd_raw.values())
    window_total = round(window_total_raw, 2)          # 表示用
    provider_rows = [
        {"provider": p, "usd": round(u, 2),
         "pct": round(u / window_total_raw * 100, 1) if window_total_raw else 0.0}
        for p, u in sorted(prov_usd_total.items(), key=lambda kv: -kv[1])
    ]

    # daily trend with 前日比
    trend = []
    prev = None
    for d in dates_sorted:
        usd = daily_usd[d]
        delta_pct = round((usd - prev) / prev * 100, 0) if (prev and prev > 0) else None
        trend.append({"date": d, "usd": usd, "delta_pct": delta_pct,
                      "providers": daily_by_provider[d]})
        prev = usd

    # cache 効率 (= Anthropic Opus の prompt caching が効いてるか、調査の核心指標)
    anth_in = anth_cr = anth_cw = 0
    for m, tk in by_model.items():
        if _cost_provider(m).startswith("Anthropic"):
            anth_in += tk["input"]
            anth_cr += tk["cache_read"]
            anth_cw += tk["cache_write"]
    total_anth_in = anth_in + anth_cr + anth_cw
    cache_hit_pct = round(anth_cr / total_anth_in * 100, 1) if total_anth_in else 0.0

    # totals / projection
    n_days = max(1, len(dates_sorted))
    avg_daily = round(window_total / n_days, 2)
    total_calls = sum(r["calls"] for r in model_rows)
    today_str = datetime.now(JST).strftime("%Y-%m-%d")
    budget_cap = float(os.getenv("LITELLM_MAX_BUDGET", "50") or 50)
    today_usd = daily_usd.get(today_str, 0.0)

    return {
        "has_usage_data": True,
        "window_sec": since_sec,
        "window_label": _format_window_label(since_sec),
        "generated_at": datetime.now(JST).isoformat(timespec="seconds"),
        "totals": {
            "usd": window_total,
            "calls": total_calls,
            "avg_daily_usd": avg_daily,
            "monthly_projection_usd": round(avg_daily * 30, 2),
            "n_days": n_days,
        },
        "today": {"date": today_str, "usd": round(today_usd, 2)},
        "budget": {
            "cap_usd": budget_cap,
            "today_pct": round(today_usd / budget_cap * 100, 1) if budget_cap else 0.0,
        },
        "by_provider": provider_rows,
        "by_model": model_rows,
        "by_component": comp_rows,
        "daily_trend": trend,
        "cache": {
            "anthropic_input_tokens": anth_in,
            "anthropic_cache_read_tokens": anth_cr,
            "anthropic_cache_write_tokens": anth_cw,
            "cache_hit_pct": cache_hit_pct,
        },
    }


def render_dashboard_html(data: dict) -> str:
    """JSON aggregate を 海山読みやすい HTML dashboard に render."""
    summary = data.get("summary", {})
    ch = data.get("channel_split", {})
    roi = data.get("roi_progress", {})
    components = data.get("components", {})
    top_users = data.get("top_users", [])
    daily_trend = data.get("daily_trend", [])

    # ROI bar
    progress_pct = roi.get("progress_pct", 0)
    bar_width = min(100, progress_pct)
    bar_color = "#22c55e" if progress_pct >= 100 else ("#3b82f6" if progress_pct >= 50 else "#f59e0b")

    comp_rows = "".join(
        f"<tr><td>{c}</td><td>{d.get('ok', 0)}</td><td style='color:{'#dc2626' if d.get('fail') else '#666'}'>{d.get('fail', 0)}</td><td>{d.get('avg_ms', 0)}</td></tr>"
        for c, d in sorted(components.items())
    )

    user_rows = "".join(
        f"<tr><td><code>{u['user_id']}</code></td><td>{u['turns']}</td><td style='color:{'#dc2626' if u['fail'] else '#666'}'>{u['fail']}</td><td>{u['avg_ms']}</td></tr>"
        for u in top_users
    )

    # daily trend (last 14 entries shown)
    trend_rows = "".join(
        f"<tr><td>{t['date']}</td><td>{t['queries']}</td><td>{t['failures']}</td></tr>"
        for t in daily_trend[-14:]
    )

    html = f"""<!DOCTYPE html>
<html lang="ja">
<head>
<meta charset="utf-8">
<title>うみやまAI Usage Dashboard ({data.get('window_label', '')})</title>
<style>
body {{ font-family: -apple-system, BlinkMacSystemFont, "Helvetica Neue", sans-serif; max-width: 1000px; margin: 20px auto; padding: 0 16px; color: #222; }}
h1 {{ font-size: 22px; }}
h2 {{ font-size: 17px; margin-top: 32px; border-bottom: 2px solid #e5e7eb; padding-bottom: 6px; }}
.kpi {{ display: inline-block; margin-right: 28px; padding: 8px 14px; background: #f9fafb; border-radius: 6px; border-left: 3px solid #3b82f6; }}
.kpi .label {{ display: block; font-size: 11px; color: #6b7280; }}
.kpi .value {{ display: block; font-size: 22px; font-weight: 600; }}
.bar-wrap {{ background: #e5e7eb; border-radius: 6px; height: 28px; margin: 8px 0; position: relative; }}
.bar {{ background: {bar_color}; height: 100%; border-radius: 6px; width: {bar_width}%; }}
.bar-label {{ position: absolute; top: 4px; left: 12px; color: #fff; font-weight: 600; font-size: 13px; }}
table {{ border-collapse: collapse; width: 100%; margin: 8px 0; font-size: 13px; }}
th, td {{ padding: 6px 10px; text-align: left; border-bottom: 1px solid #e5e7eb; }}
th {{ background: #f9fafb; font-weight: 600; }}
code {{ background: #f3f4f6; padding: 2px 5px; border-radius: 3px; font-size: 12px; }}
.meta {{ color: #6b7280; font-size: 11px; }}
.alert {{ padding: 8px 12px; background: #fef3c7; border-left: 3px solid #f59e0b; margin: 8px 0; border-radius: 4px; font-size: 13px; }}
.ok {{ color: #16a34a; }}
.warn {{ color: #d97706; }}
.bad {{ color: #dc2626; }}
</style>
</head>
<body>
<h1>うみやまAI Usage Dashboard</h1>
<p class="meta">window: {data.get('window_label', '')} | generated: {data.get('generated_at', '')}</p>

<h2>Phase 1 ROI Progress</h2>
<div class="bar-wrap">
  <div class="bar"></div>
  <div class="bar-label">{progress_pct}% (= 月間 pace {roi.get('current_pace_estimate_monthly', 0)} / target 1,000)</div>
</div>
<p class="meta">Ultimate target (Phase 1 full): {roi.get('phase_1_full_target', 0):,} queries / 月 | 1 日平均: {roi.get('queries_per_day_avg', 0)} queries/day</p>

<h2>Summary</h2>
<div class="kpi"><span class="label">total queries</span><span class="value">{summary.get('total_queries', 0):,}</span></div>
<div class="kpi"><span class="label">replied</span><span class="value">{summary.get('total_replies', 0):,}</span></div>
<div class="kpi"><span class="label">failure rate</span><span class="value">{summary.get('failure_rate_pct', 0)}%</span></div>
<div class="kpi"><span class="label">avg latency</span><span class="value">{summary.get('avg_latency_ms', 0):,}ms</span></div>
<div class="kpi"><span class="label">p95 latency</span><span class="value">{summary.get('p95_latency_ms', 0):,}ms</span></div>

<h2>Channel Split (DM vs Group)</h2>
<div class="kpi"><span class="label">DM</span><span class="value">{ch.get('dm_count', 0):,}</span></div>
<div class="kpi"><span class="label">Group</span><span class="value">{ch.get('group_count', 0):,}</span></div>
<div class="kpi"><span class="label">Group %</span><span class="value">{ch.get('group_pct', 0)}%</span></div>

<h2>Top Users (heavy 検出)</h2>
<table>
<tr><th>user_id</th><th>turns</th><th>fail</th><th>avg_ms</th></tr>
{user_rows or '<tr><td colspan="4" class="meta">(no user data)</td></tr>'}
</table>

<h2>Components</h2>
<table>
<tr><th>component</th><th>ok</th><th>fail</th><th>avg_ms</th></tr>
{comp_rows or '<tr><td colspan="4" class="meta">(no component data)</td></tr>'}
</table>

<h2>Daily Trend (last 14 days)</h2>
<table>
<tr><th>date</th><th>queries</th><th>failures</th></tr>
{trend_rows or '<tr><td colspan="3" class="meta">(no daily data)</td></tr>'}
</table>

<p class="meta" style="margin-top: 40px;">★2026-05-24 Feature 2/4 Usage Dashboard | data source: data/brain/bot_events/events.jsonl</p>
</body>
</html>
"""
    return html
