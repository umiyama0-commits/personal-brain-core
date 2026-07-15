#!/usr/bin/env bash
# quality_metrics_cron.sh — 日次 品質 metric 集計 + 劣化 trend alert
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# ★2026-05-26 海山 (C2+C3) 指示: 応答品質の 24h baseline 劣化を LINE Push。
#
# cron entry:
#   5 4 * * * $REPO_ROOT/scripts/quality_metrics_cron.sh ...
#
# タイミング根拠:
#   - 04:00 privacy-review
#   - 04:05 quality_metrics (= 直近 24h の集計、privacy 完了後)
#   - 04:30 monthly 月初 alignment_snapshot
#
# 動作:
#   1. cron_env.sh source (= PATH / .env)
#   2. python3 scripts/quality_metrics.py
#   3. 劣化検知時に LINE Push (= 24h cooldown 内蔵)
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
set -uo pipefail

source "$(dirname "$0")/cron_env.sh"

REPO_ROOT="${REPO_ROOT:-/Users/brain/brain-agent}"
cd "$REPO_ROOT" || { echo "ERROR: cd $REPO_ROOT failed"; exit 2; }

echo "$(date): ===== quality_metrics run start ====="
python3 scripts/quality_metrics.py
RC=$?
echo "$(date): ===== quality_metrics done (rc=$RC) ====="
exit 0  # alert 検知 (rc=1) でも cron は success 扱い (= LINE Push が代替通知)
