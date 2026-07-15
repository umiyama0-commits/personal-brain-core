#!/usr/bin/env bash
# magazine_backfill_cron.sh — もぐもぐダイアリー backfill orchestrator の cron wrapper
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# ★2026-07-06: 過去号 backfill を git 配送で遠隔実行するための毎時 cycle。
#   リクエスト無し / 完了済みなら即 exit (無音) なので常設して害が無い。
#   stapa login creds (.env の STAPA_USER/PASS) と LITELLM を使うため
#   stapa_cron.sh と同じく .env + cron_env.sh を明示 source (CLAUDE.md 3.6)。
#
# cron entry (scripts/cron_install.sh で自動登録):
#   15 8-22 * * * $REPO_ROOT/scripts/magazine_backfill_cron.sh >> $REPO_ROOT/data/brain/scrape.log 2>&1
#
# dry-run (cron 登録前の必須確認、CLAUDE.md 1.8):
#   env PATH="/usr/bin:/bin" bash scripts/magazine_backfill_cron.sh --dry-run
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
set -uo pipefail

REPO_ROOT="${REPO_ROOT:-/Users/brain/brain-agent}"
cd "$REPO_ROOT" || { echo "ERROR: cd $REPO_ROOT failed"; exit 2; }

if [ -f ./.env ]; then
    set -a
    # shellcheck disable=SC1091
    . ./.env
    set +a
fi

# shellcheck disable=SC1091
source "$(dirname "$0")/cron_env.sh"

python3 scripts/magazine_backfill.py "$@"
exit $?
