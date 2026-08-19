#!/usr/bin/env bash
# personal_sync_cron.sh — Claude.ai の Example 会話を personal ドメインへ取り込む wrapper (★2026-06-28)。
# Playwright headed (bot 検知回避) = GUI/Aqua セッション必須 → crontab でなく LaunchAgent
# com.brain.personal-sync から実行する (cron 文脈は GUI 不可)。cron_env.sh で .env(LITELLM)+PATH を source。
set -uo pipefail

source "$(dirname "$0")/cron_env.sh"

REPO_ROOT="${REPO_ROOT:-/Users/brain/brain-agent}"
cd "$REPO_ROOT" || { echo "ERROR: cd $REPO_ROOT failed"; exit 2; }

echo "$(date): ===== example_sync start ====="
python3 scripts/claude_personal_sync.py --count 20
RC=$?
echo "$(date): ===== example_sync done (rc=$RC) ====="
exit $RC
