"""
clone_usage_metrics.py — うみやまAI の日次利用トラッキング

会話ログ (clone_history) を集計して `data/brain/metrics/daily/YYYY-MM-DD.json` に保存。
個人特定可能な質問内容は出力しない (集計値とトピック分類のみ)。

cron: 毎日 02:30 JST
  python3 scripts/clone_usage_metrics.py

LLM トピック分類は fast-gpt (GPT-5.4-mini、軽量) で実行 (★2026-06-07 評価: smart-gpt から cost 最適化)。
"""
from __future__ import annotations

import asyncio
import json
import logging
import re
import sys
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from clone_improve_lib import (
    ensure_dirs, load_conversations, group_by_session,
    call_llm, extract_json, METRICS_DIR, JST,
)

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger("clone_usage_metrics")

SATISFACTION_SIGNALS = (
    "ありがとう", "助かった", "OK", "わかった", "了解", "ありがと", "サンキュー",
    "助かる", "良いね", "いいね", "なるほど", "そうそう",
)
CORRECTION_SIGNALS = (
    "違う", "ちがう", "間違", "正しくは", "訂正", "事実と違う", "そうじゃない", "誤り",
)
KNOWLEDGE_GAP_PHRASES = (
    "こっちに流し込めてない", "こっちに入ってない", "wikiに入ってない", "データが無い",
    "情報が無い", "情報なし", "該当情報", "確認できない", "分からない", "わからない",
    "聞いていない", "情報をまだ持ってない",
)
TOPIC_CATEGORIES = [
    "店舗運営", "人事制度", "判断相談", "数値確認",
    "戦略", "海外", "雑談", "その他",
]


def _is_correction(text: str) -> bool:
    return any(p in text for p in CORRECTION_SIGNALS)


def _is_knowledge_gap_reply(text: str) -> bool:
    return any(p in text for p in KNOWLEDGE_GAP_PHRASES)


def _is_satisfaction(text: str) -> bool:
    return any(p in text for p in SATISFACTION_SIGNALS)


def split_bot_served(records: list[dict]) -> tuple[list[dict], int]:
    """bot が実際に応答した record と、グループの silent listen 分を分離する。

    ★2026-08-10 (再ローンチ総点検 critical): 利用実態が 3 倍に膨れていた。
    実測 30 日: 「292 件/24 人」のうち 199 件はグループの人間同士の雑談で、
    bot は 1 件も返答していない (main.py の group handler が silent listen で
    channel_id 付き record を積む設計)。実利用は DM 94 件/17 人。
    KPI の分母は「bot が応答した質問」でなければ、再ローンチの効果測定が歪む。
    判定: channel_id 無し (DM) は全部 / channel_id 有りは assistant 応答が
    存在する channel のみを「bot 利用」と数える。
    """
    dm = [r for r in records if not r.get("channel_id")]
    served_channels = {r.get("channel_id") for r in records
                       if r.get("channel_id") and r.get("role") == "assistant"}
    grp_served = [r for r in records
                  if r.get("channel_id") and r.get("channel_id") in served_channels]
    listen_only = sum(1 for r in records
                      if r.get("channel_id") and r.get("channel_id") not in served_channels)
    return dm + grp_served, listen_only


def calc_volume_depth(records: list[dict]) -> dict:
    records, group_listen_turns = split_bot_served(records)
    sessions = group_by_session(records, gap_minutes=30)
    user_turns = [r for r in records if r.get("role") == "user"]
    unique_users = set(r.get("user_id") for r in records if r.get("user_id"))
    deep = sum(1 for s in sessions if sum(1 for r in s if r.get("role") == "user") >= 5)
    one_shot = sum(1 for s in sessions if sum(1 for r in s if r.get("role") == "user") == 1)

    # abandon_rate: AI 回答後に user の追加発言が無いセッション
    abandon = 0
    for s in sessions:
        last = s[-1] if s else None
        if last and last.get("role") == "assistant":
            abandon += 1
    abandon_rate = round(abandon / len(sessions), 3) if sessions else 0

    avg_turns = round(len(user_turns) / max(1, len(sessions)), 1)
    # グループ silent listen (bot 非応答) は KPI 分母から除外し、参考値としてのみ持つ

    # by_hour
    by_hour = [0] * 24
    for r in user_turns:
        try:
            ts = datetime.fromisoformat(r.get("timestamp", "").replace("Z", "+00:00"))
            ts_j = ts.astimezone(JST)
            by_hour[ts_j.hour] += 1
        except Exception:
            pass
    peak_hour = by_hour.index(max(by_hour)) if max(by_hour) > 0 else None

    return {
        "group_listen_turns": group_listen_turns,
        "volume": {
            "total_conversations": len(sessions),
            "total_turns": len(user_turns),
            "unique_users": len(unique_users),
            "avg_turns_per_session": avg_turns,
        },
        "depth": {
            "deep_sessions": deep,
            "one_shot_sessions": one_shot,
            "abandon_rate": abandon_rate,
        },
        "_by_hour": by_hour,
        "_peak_hour": peak_hour,
    }


def calc_quality(records: list[dict]) -> dict:
    user_turns = [r for r in records if r.get("role") == "user"]
    ai_turns = [r for r in records if r.get("role") == "assistant"]
    gap = sum(1 for r in ai_turns if _is_knowledge_gap_reply(r.get("text", "")))
    corr = sum(1 for r in user_turns if _is_correction(r.get("text", "")))
    satisf = sum(1 for r in user_turns if _is_satisfaction(r.get("text", "")))

    # rephrase_retry: 同じ user_id が短時間内に類似 query を再投 (シンプル: 24h 内に同じ 5-gram)
    rephrase = 0
    by_user: dict[str, list[str]] = defaultdict(list)
    for r in user_turns:
        uid = r.get("user_id")
        if uid:
            by_user[uid].append(r.get("text", ""))
    for uid, texts in by_user.items():
        if len(texts) < 2:
            continue
        seen_ngrams = set()
        for t in texts:
            tokens = list(t)
            if len(tokens) < 5:
                continue
            for i in range(len(tokens) - 4):
                ng = "".join(tokens[i:i+5])
                if ng in seen_ngrams:
                    rephrase += 1
                    break
                seen_ngrams.add(ng)
    return {
        "quality": {
            "knowledge_gap_rate": round(gap / max(1, len(ai_turns)), 3),
            "rephrase_retry_rate": round(rephrase / max(1, len(user_turns)), 3),
            "correction_rate": round(corr / max(1, len(user_turns)), 3),
            "satisfaction_signals": satisf,
        }
    }


def calc_users(records: list[dict], previous_users: set[str], lookback_active_users: set[str]) -> dict:
    user_turns = defaultdict(int)
    for r in records:
        if r.get("role") == "user" and r.get("user_id"):
            user_turns[r["user_id"]] += 1
    power = [u for u, c in user_turns.items() if c >= 5]
    today_active = set(user_turns.keys())
    new = sorted(today_active - previous_users)
    dormant = sorted(lookback_active_users - today_active)
    return {
        "users": {
            "power_users_count": len(power),
            "power_users": sorted(power)[:20],
            "new_users_count": len(new),
            "new_users": new[:20],
            "dormant_users_count": len(dormant),
        }
    }


async def classify_topics(records: list[dict]) -> dict:
    """user 質問を LLM でトピック分類 (個人特定情報は出さない)。"""
    user_texts = [r.get("text", "")[:100] for r in records if r.get("role") == "user"]
    if not user_texts:
        return {"topics": {"distribution": {}, "top_questions": []}}
    sample = user_texts[:120]
    prompt = f"""以下は社員から うみやまAI への質問テキストのリストです。
これらを 8 カテゴリ (店舗運営 / 人事制度 / 判断相談 / 数値確認 / 戦略 / 海外 / 雑談 / その他) に分類し、
さらに頻出パターンを抽出してください。

【質問リスト】
{json.dumps(sample, ensure_ascii=False)}

【出力 JSON のみ】
{{
  "distribution": {{ "店舗運営": <件数>, "人事制度": <件数>, ... }},
  "top_questions": [
    {{"cluster": "<簡潔な分類名>", "count": <件数>}},
    ...
  ]
}}

★ 個人特定情報・実名・店舗名は出力しない。集計値とクラスタ名のみ。
★ クラスタは最大 10 個、count >= 2 のもののみ。
"""
    try:
        # ★2026-06-07 エージェント評価: topic 分類は軽量 tier で十分。§4 では軽量=fast-gpt(GPT-5.4-mini)、
        #   smart-gpt(GPT-5.4) は self-eval 分離/比較用。日次120件分類に smart-gpt は過剰 → fast-gpt に。
        out = await call_llm(prompt, model="fast-gpt", max_tokens=2000)
        data = extract_json(out)
        return {"topics": data}
    except Exception as e:
        logger.warning(f"topic classification failed: {e}")
        return {"topics": {"distribution": {}, "top_questions": []}}


def load_prev_metrics(date: datetime) -> dict:
    prev = date - timedelta(days=1)
    p = METRICS_DIR / f"{prev.strftime('%Y-%m-%d')}.json"
    if p.exists():
        try:
            return json.loads(p.read_text(encoding="utf-8"))
        except Exception:
            pass
    return {}


def calc_delta(curr: dict, prev: dict) -> dict:
    """前日比 delta を計算。"""
    deltas = {}
    def _d(curr_v, prev_v, pct=False, anomaly_threshold=0.5):
        if prev_v in (None, 0):
            return None
        if pct:
            ratio = (curr_v - prev_v) / max(1, prev_v)
            return f"{ratio*100:+.1f}%"
        return f"{curr_v - prev_v:+d}"

    pv = prev.get("volume", {})
    cv = curr.get("volume", {})
    deltas["volume"] = {
        "total_conversations": _d(cv.get("total_conversations", 0), pv.get("total_conversations", 0), pct=True),
        "unique_users": _d(cv.get("unique_users", 0), pv.get("unique_users", 0)),
    }
    return deltas


def detect_anomalies(curr: dict, prev: dict) -> list[dict]:
    """前日比 ±50% 超 の指標を anomaly として記録。"""
    anomalies = []
    def _check(label, c, p, threshold=0.5):
        if p in (None, 0):
            return
        ratio = abs((c - p) / max(1, p))
        if ratio >= threshold:
            anomalies.append({
                "metric": label, "value": c, "prev_day": p,
                "ratio": round(ratio, 2),
            })
    _check("total_conversations",
           curr.get("volume", {}).get("total_conversations", 0),
           prev.get("volume", {}).get("total_conversations", 0))
    _check("knowledge_gap_rate",
           curr.get("quality", {}).get("knowledge_gap_rate", 0),
           prev.get("quality", {}).get("knowledge_gap_rate", 0))
    _check("abandon_rate",
           curr.get("depth", {}).get("abandon_rate", 0),
           prev.get("depth", {}).get("abandon_rate", 0))
    return anomalies


async def main():
    ensure_dirs()
    # ★2026-05-27 海山指示: --date YYYY-MM-DD で過去日 backfill 可能化
    # (= cron 失敗で 5/26 以前の daily metrics 全滅、retroactive 生成用)
    import argparse
    parser = argparse.ArgumentParser()
    parser.add_argument("--date", type=str, default=None,
                        help="集計対象日 YYYY-MM-DD (= default は昨日、backfill 用)")
    args, _unknown = parser.parse_known_args()

    today = datetime.now(JST).date()
    if args.date:
        # 過去日 backfill: 指定日 00:00 〜 翌日 00:00 を window に
        from datetime import date as _date
        target = _date.fromisoformat(args.date)
        since = datetime.combine(target, datetime.min.time(), tzinfo=JST)
        until = datetime.combine(target + timedelta(days=1), datetime.min.time(), tzinfo=JST)
        target_date = args.date
    else:
        since = datetime.combine(today, datetime.min.time(), tzinfo=JST) - timedelta(days=1)
        until = datetime.combine(today, datetime.min.time(), tzinfo=JST)
        target_date = (today - timedelta(days=1)).strftime("%Y-%m-%d")

    logger.info(f"=== clone_usage_metrics for {target_date} ===")
    records = [r for r in load_conversations(since) if r.get("timestamp", "") < until.isoformat()]
    logger.info(f"records: {len(records)}")

    # 前日 metrics で diff 用
    prev = load_prev_metrics(datetime.now(JST))

    result = {
        "date": target_date,
        "weekday": (today - timedelta(days=1)).strftime("%a"),
    }
    vd = calc_volume_depth(records)
    by_hour = vd.pop("_by_hour")
    peak_hour = vd.pop("_peak_hour")
    result.update(vd)
    result.update(calc_quality(records))

    # 前日アクティブ user (dormant 判定用)
    lookback_since = datetime.now(JST) - timedelta(days=7)
    lookback_records = load_conversations(lookback_since)
    lookback_users = set(r.get("user_id") for r in lookback_records if r.get("role") == "user" and r.get("user_id"))
    prev_users = set(prev.get("users", {}).get("power_users", []) + prev.get("users", {}).get("new_users", []))
    result.update(calc_users(records, prev_users, lookback_users))

    # topic 分類 (LLM)
    topics = await classify_topics(records)
    result.update(topics)

    # segments
    result["segments"] = {
        "by_hour": by_hour,
        "active_hour_peak": f"{peak_hour:02d}:00-{(peak_hour+1)%24:02d}:00" if peak_hour is not None else None,
    }

    # delta + anomalies
    result["delta_vs_prev_day"] = calc_delta(result, prev)
    result["anomalies"] = detect_anomalies(result, prev)

    out_path = METRICS_DIR / f"{target_date}.json"
    out_path.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    logger.info(f"wrote {out_path}")

    # ★2026-06-07 エージェント評価: anomaly 検知 (前日比±50%超) を LINE Push に繋ぐ。
    #   従来は JSON に残すだけで silent = knowledge_gap/abandon 急増 (品質劣化 signal) を見逃していた。
    anomalies = result.get("anomalies", [])
    if anomalies:
        try:
            from clone_improve_lib import line_push, line_push_digest  # type: ignore
            msg = [f"📊 利用metrics anomaly ({target_date}、前日比±50%超):"]
            for a in anomalies:
                msg.append(f"  {a['metric']}: {a['prev_day']} → {a['value']} (×{a['ratio']})")
            line_push_digest("\n".join(msg), "利用集計")
        except Exception as e:
            logger.warning(f"anomaly push failed: {e}")
    # cron 成否を bot_events に記録 (= 過去の silent cron fail 事故対策、observability 統一)
    try:
        from bot_events import log_bot_event  # type: ignore
        log_bot_event("usage_metrics", "turn_finished",
                      date=target_date, n_records=len(records), n_anomalies=len(anomalies))
    except Exception:
        pass

    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    asyncio.run(main())
