#!/usr/bin/env bash
# kpi_dash_cron.sh — kpi-dash.com Dashboard 週次 scrape の cron wrapper
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# ★2026-05-25 海山指示: 月曜 18:00 JST に kpi-dash.com Dashboard 全 KPI を scrape。
#
# cron entry (scripts/cron_install.sh で自動登録):
#   0 18 * * 1 $REPO_ROOT/scripts/kpi_dash_cron.sh >> $LOG_DIR/cron.log 2>&1
#
# 動作:
#   1. cron_env.sh source (= PATH / .env / LITELLM_URL の 3 点セット)
#   2. kpi_dash_scraper.py --fetch を実行
#   3. exit code: 0=成功 / 非 0=login or scrape 失敗
#
# 手動実行:
#   bash scripts/kpi_dash_cron.sh
#   bash scripts/kpi_dash_cron.sh --dry-run   # ファイル保存しない
#
# dry-run (cron 登録前の必須確認、CLAUDE.md 1.8):
#   env -i PATH="/usr/bin:/bin" bash scripts/kpi_dash_cron.sh --dry-run
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
set -uo pipefail

# cron 3 点セット (PATH / .env / LITELLM_URL) を source
source "$(dirname "$0")/cron_env.sh"

REPO_ROOT="${REPO_ROOT:-/Users/brain/brain-agent}"
cd "$REPO_ROOT" || { echo "ERROR: cd $REPO_ROOT failed"; exit 2; }

DRY_RUN_FLAG=""
if [ "${1:-}" = "--dry-run" ]; then
    DRY_RUN_FLAG="--dry-run"
fi

echo "$(date): ===== kpi-dash scrape start ====="

# ★2026-06-10 fix: kpi-dash は password-only login (user field 無し、★2026-05-25 設計)。
# scraper 側 (kpi_dash_scraper.py L80) は KPIDASH_USER 任意なのに、本 guard が両方要求して
# 5/25 以来 毎週 silent skip していた (= monday-dash-latest の kpi section が stale 化した真因)。
if [ -z "${KPIDASH_PASS:-}" ]; then
    echo "$(date): ERROR: KPIDASH_PASS が .env に未設定" >&2
    exit 3
fi

python3 kpi_dash_scraper.py --fetch $DRY_RUN_FLAG
RC=$?

echo "$(date): ===== kpi-dash scrape done (rc=$RC) ====="
exit $RC
