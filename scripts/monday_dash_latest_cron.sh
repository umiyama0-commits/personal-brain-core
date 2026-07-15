#!/usr/bin/env bash
# monday_dash_latest_cron.sh — Monday Dash latest 集約 wiki の rebuild cron wrapper
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# ★2026-05-25 海山指示: Monday Dash = 最重要、最新鮮度 high。
#
# cron entry (scripts/cron_install.sh で自動登録):
#   0 3 * * 2 $REPO_ROOT/scripts/monday_dash_latest_cron.sh ...    # 火曜 03:00 (第一試行)
#   0 3 * * 3 $REPO_ROOT/scripts/monday_dash_latest_cron.sh ...    # 水曜 03:00 (保険、火曜失敗時)
#
# タイミング根拠:
#   - 月曜 18:00 kpi_dash_scraper → kpi_dash_dashboard_YYYY-MM-DD.md
#   - 月曜 21:00 gdrive_sync      → gdrive_monday-dash_*.md
#   - 月曜 22:00 lineworks_scraper → lineworks_Monday_Dash_YYYY-MM-DD.md (火朝の数字 message を含む日も)
#   - 火曜 03:00 が全 source 完了後の最初の安定 window
#   - 水曜 03:00 は 1 回 fail で 1 週間取り損ねないための冗長
#
# 動作:
#   1. cron_env.sh source (= PATH / .env / LITELLM_URL 3 点)
#   2. python3 scripts/build_monday_dash_latest.py を実行
#   3. data/brain/wiki/knowledge/owndays-monday-dash-latest.md を上書き
#
# 手動実行:
#   bash scripts/monday_dash_latest_cron.sh
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
set -uo pipefail

source "$(dirname "$0")/cron_env.sh"

REPO_ROOT="${REPO_ROOT:-/Users/brain/brain-agent}"
cd "$REPO_ROOT" || { echo "ERROR: cd $REPO_ROOT failed"; exit 2; }

echo "$(date): ===== monday_dash_latest rebuild start ====="
python3 scripts/build_monday_dash_latest.py
RC=$?
echo "$(date): ===== monday_dash_latest rebuild done (rc=$RC) ====="
exit $RC
