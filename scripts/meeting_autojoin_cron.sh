#!/bin/bash
# meeting_autojoin_cron.sh — web会議 自動参加 + 議事録回収 (★2026-07-03)
# cron: */10 7-22 * * * (10分毎、営業時間帯)
set -uo pipefail
cd /Users/brain/brain-agent || exit 1
# cron 3点セット (PATH + .env + LITELLM_URL) — §1.8/§3.6
[ -f scripts/cron_env.sh ] && . scripts/cron_env.sh 2>/dev/null || true
if [ -f .env ]; then set -a; . ./.env; set +a; fi

# 多重起動防止 (macOS に flock コマンドが無いため mkdir lock + 30分 staleness)
LOCK_DIR="/tmp/meeting_autojoin.lock.d"
if ! mkdir "$LOCK_DIR" 2>/dev/null; then
    # 30分より古い lock は前回異常終了の残骸 → 奪取 (rmdir+mkdir で原子的に取り直す)
    if [ -n "$(find "$LOCK_DIR" -maxdepth 0 -mmin +30 2>/dev/null)" ]; then
        rmdir "$LOCK_DIR" 2>/dev/null || true
        if ! mkdir "$LOCK_DIR" 2>/dev/null; then
            echo "$(date): stale 奪取 race 負け → skip"
            exit 0
        fi
        echo "$(date): stale lock 奪取"
    else
        echo "$(date): 前 cycle 実行中 → skip"
        exit 0
    fi
fi
trap 'rmdir "$LOCK_DIR" 2>/dev/null || true' EXIT
touch "$LOCK_DIR" 2>/dev/null || true

/usr/bin/python3 scripts/meeting_autojoin.py "$@"
