#!/usr/bin/env bash
# style_reflux_cron.sh — 週次 audit/feedback/learning → style 改善 proposal
# ★2026-05-26 海山 B1+B3「audit fail / feedback を style へ逆流」 (= 品質改善 top 3)
#
# cron entry:
#   10 4 * * 1 $REPO_ROOT/scripts/style_reflux_cron.sh ...
#
# タイミング: 月曜 04:10 (= quality_metrics 04:05 後、海山が朝チェック前に揃ってる)
set -uo pipefail

source "$(dirname "$0")/cron_env.sh"

REPO_ROOT="${REPO_ROOT:-/Users/brain/brain-agent}"
cd "$REPO_ROOT" || { echo "ERROR: cd $REPO_ROOT failed"; exit 2; }

echo "$(date): ===== style_reflux run start ====="
python3 scripts/style_reflux.py
RC=$?
echo "$(date): ===== style_reflux done (rc=$RC) ====="
exit $RC
