#!/usr/bin/env bash
# stores_by_customer_range_cron.sh — 日本店舗 客数 range 別 index の rebuild cron wrapper
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# ★2026-05-25 海山指示の daily cron 登録。
# 「日本で月間 350 客くらいの店」 等の数値 range filter query 用 pre-computed index。
#
# cron entry (scripts/cron_install.sh で自動登録):
#   35 23 * * * $REPO_ROOT/scripts/stores_by_customer_range_cron.sh ...
#
# タイミング根拠:
#   - 23:00 (Sun のみ) mobile_owndays_historical FULL refresh
#   - 23:15 (daily) build_breakdown_history (= nation/area/type/league)
#   - 23:30 (Sun のみ) historical 完了後の post-process 余白
#   - 23:35 (daily) ここで stores-by-range を再生成 (= 直近 2 月の Japan 店舗を bucket 化)
#
# 動作:
#   1. cron_env.sh source (= PATH / .env / LITELLM_URL)
#   2. python3 scripts/build_stores_by_customer_range.py
#   3. data/brain/wiki/knowledge/owndays-stores-by-customer-range.md を上書き
#
# 手動実行:
#   bash scripts/stores_by_customer_range_cron.sh
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
set -uo pipefail

source "$(dirname "$0")/cron_env.sh"

REPO_ROOT="${REPO_ROOT:-/Users/brain/brain-agent}"
cd "$REPO_ROOT" || { echo "ERROR: cd $REPO_ROOT failed"; exit 2; }

echo "$(date): ===== stores_by_customer_range rebuild start ====="
python3 scripts/build_stores_by_customer_range.py
RC=$?
echo "$(date): ===== stores_by_customer_range rebuild done (rc=$RC) ====="
exit $RC
