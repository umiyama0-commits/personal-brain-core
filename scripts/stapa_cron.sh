#!/usr/bin/env bash
# stapa_cron.sh — STAPA OWNDAYS MAGAZINE スクレイパーの cron wrapper
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# ★2026-06-10 運用堅牢化: crontab の stapa_scraper.py が bare `python3 ... ` で
#   .env を source しておらず、os.getenv("STAPA_USER"/"STAPA_PASS") が空のまま
#   ログイン → 無音失敗していた (stapa_scraper.py は creds 欠落でも warn のみで継続)。
#   CLAUDE.md 3.6 (scrape_cron.sh の .env source 原則) と同じく、wrapper 冒頭で
#   明示的に .env を source し、creds を環境へ展開してから実行する。
#
# cron entry (scripts/cron_install.sh で自動登録):
#   30 22 */14 * * $REPO_ROOT/scripts/stapa_cron.sh >> $REPO_ROOT/data/brain/scrape.log 2>&1
#   (= 従来の手動 crontab と同じ 22:30 / 14 日おき schedule を踏襲)
#
# 引数はそのまま stapa_scraper.py へ pass-through (--all / --dry-run)。
#
# 手動実行:
#   bash scripts/stapa_cron.sh
#   bash scripts/stapa_cron.sh --dry-run   # プレビューのみ (保存しない)
#
# dry-run (cron 登録前の必須確認、CLAUDE.md 1.8):
#   env PATH="/usr/bin:/bin" bash scripts/stapa_cron.sh --dry-run
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
set -uo pipefail

REPO_ROOT="${REPO_ROOT:-/Users/brain/brain-agent}"
cd "$REPO_ROOT" || { echo "ERROR: cd $REPO_ROOT failed"; exit 2; }

# ★まず .env を明示 source (CLAUDE.md 3.6: cron は親 shell env を継承しない)。
if [ -f ./.env ]; then
    set -a
    # shellcheck disable=SC1091
    . ./.env
    set +a
fi

# cron 3 点セット (PATH / .env / LITELLM_URL) も source (PATH に chrome/playwright 解決用)。
# shellcheck disable=SC1091
source "$(dirname "$0")/cron_env.sh"

echo "$(date): ===== stapa scrape start ====="

# creds 欠落を loud に出す (無音失敗を防ぐ)。scraper 自体は継続するが、
# ここで明示しておけば cron ログ grep で即気付ける。
if [ -z "${STAPA_USER:-}" ] || [ -z "${STAPA_PASS:-}" ]; then
    echo "$(date): ⚠️ STAPA_USER / STAPA_PASS が未設定 (.env に無い)。ログイン失敗の見込み。" >&2
fi

python3 stapa_scraper.py "$@"
RC=$?

echo "$(date): ===== stapa scrape done (rc=$RC) ====="
exit $RC
