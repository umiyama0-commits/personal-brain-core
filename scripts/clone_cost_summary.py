"""
clone_cost_summary.py — LiteLLM 経由の日次 cost を LINE Push (★2026-05-23 LEE §4.2)

cron: 09:00 JST 毎日
1. bot_metrics._format_cost_summary を呼んで model 別 USD 集計
2. 結果を LINE Push (admin only)
3. 前日比 +30% 以上で警告マーク済 (= _format_cost_summary 内で実装)

★2026-07-10 (世界基準評価 #5) 訂正: litellm_config.yaml の max_budget (50 USD/日) は
**DB(Postgres)無しでは inert** で hard cap になっていない (2026-06-11 failure-log 実証)。
真の hard cap は (a) 各 provider console の spend limit (海山が設定=即日・コード変更ゼロ)、
(b) litellm に Postgres を足して max_budget を実効化 (別途、要海山立会いの infra 変更)。
本 script は observability 側で「今日いくら使ったか」を判定し、閾値超過は critical で通知する
(= 止め手が翌朝 alert のみだった穴を、少なくとも「確実に届く alert」に強化)。
"""
from __future__ import annotations

import logging
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from bot_metrics import _read_events, _format_cost_summary, parse_since  # noqa: E402
from clone_improve_lib import line_push  # noqa: E402

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger("clone_cost_summary")


def main() -> int:
    # 直近 7 日 (= 前日比 +30% 判定に必要なため最低 2 日)
    since_sec = parse_since("7d")
    events = _read_events(since_sec)
    summary = _format_cost_summary(events)
    print(summary)

    # ★2026-06-11 閾値超過の強調 (6/10 の $700級スパイクを当日検知できなかった反省。
    # LiteLLM /spend は DB 無しで 400 のため events.jsonl 由来の下限推定で判定)
    header = "💰 LiteLLM 日次コスト"
    try:
        import os
        import sys as _sys
        _root = str(Path(__file__).resolve().parent.parent)
        if _root not in _sys.path:
            _sys.path.insert(0, _root)
        from services.usage_analytics import aggregate_cost  # noqa: E402
        threshold = float(os.getenv("COST_ALERT_DAILY_USD", "40") or 40)
        day = aggregate_cost(since_sec=86400)
        day_usd = float((day.get("totals") or {}).get("usd") or 0)
        if threshold > 0 and day_usd > threshold:
            header = (f"🚨 日次コスト閾値超過: ${day_usd:.0f} > ${threshold:.0f}"
                      f" (24h、events下限推定)\n" + header)
    except Exception as e:
        logger.warning(f"threshold check skipped: {e}")

    # ★2026-06-11 海山指示「通知の数は減らしてよい」: 毎朝の無条件 push (30通/月、
    # 無料枠200の主犯) をやめ、push は 🚨閾値超過 or 月曜ダイジェスト のみ。
    # それ以外の日は cron.log への print (上の print(summary)) だけ残す。
    from datetime import datetime, timedelta, timezone
    is_monday = datetime.now(timezone(timedelta(hours=9))).weekday() == 0
    exceeded = header.startswith("🚨")
    if not (exceeded or is_monday):
        logger.info("LINE Push: skipped (平常日、閾値内 → 通知ダイエット)")
        return 0

    push_text = header + "\n" + summary
    # 上限 4500 字 (LINE 1 message)、超えたら truncate
    if len(push_text) > 4500:
        push_text = push_text[:4400] + "\n\n... (truncated)"
    # ★2026-07-10 (世界基準評価 #5): 閾値超過 (= お金が漏れている) は配達保証必須 → critical。
    #   非 critical だと personal LINE 日次 cap 枯渇日に spike alert が drop する (お金が漏れて
    #   いる時に限って通知が消える worst case)。月曜ダイジェストは通常通知 (非 critical)。
    ok = line_push(push_text, critical=exceeded)
    logger.info(f"LINE Push: {'sent' if ok else 'failed'} (critical={exceeded})")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
