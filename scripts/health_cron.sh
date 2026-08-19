#!/bin/bash
# health_cron.sh — 売上データパイプライン 5 層ヘルスチェック cron wrapper
#
# 05:30 JST 毎日実行する想定 (sales_accuracy_check.py の 06:00 より前)。
# crontab -e で以下を追加:
#   30 5 * * * /Users/brain/brain-agent/scripts/health_cron.sh >> /Users/brain/brain-agent/data/brain/health.log 2>&1
#
# 動作:
#   1. .env を source して LINE_CHANNEL_ACCESS_TOKEN / ALIGNMENT_TARGET_USER 等を環境に展開
#   2. scripts/sales_data_health.py を実行
#   3. exit code:
#      - 0: 全 OK (LINE Push は --verbose 時のみ)
#      - 1: いずれか NG (LINE Push 自動送信、復旧手順付き)
#
# 手動実行:
#   bash scripts/health_cron.sh                 # 通常実行 (NG なら Push)
#   bash scripts/health_cron.sh --dry-run       # Push しない
#   bash scripts/health_cron.sh --verbose       # 全 OK でも Push (動作確認用)

set -uo pipefail

REPO_DIR="$(cd "$(dirname "$0")/.." && pwd)"
cd "$REPO_DIR" || exit 99

# ★2026-05-19 根治: cron_env.sh を source (PATH + .env + LITELLM_URL の cron 3点セット)。
# 旧コードは .env のみ source で PATH を通しておらず、cron 最小 PATH では
# sales_data_health.py L3 の docker exec が "No such file or directory: 'docker'"
# → 毎朝 05:30 に bot 健全なのに誤 🚨 (オオカミ少年化)。docker-PATH バグクラスの
# documented 構造的修正 (CLAUDE.md「新 cron wrapper は冒頭で必ず cron_env.sh source」)
# にこの wrapper が未準拠だったのを是正。Python 側 _DOCKER_BIN と二重防御。
# shellcheck disable=SC1091
source "$(dirname "$0")/cron_env.sh"

echo "$(date): ===== sales_data_health start ====="
python3 scripts/sales_data_health.py "$@"
RC=$?
echo "$(date): ===== sales_data_health end (exit=$RC) ====="

# ★2026-08-03: 「要求した model が本当にその model で処理されたか」の日次検証。
# temperature 400 → 無言 gpt-4o fallback で 25日 $182 を溶かし、judge の別系列防壁も
# 無効化していた事故 (200 OK で返るため完全に無音) の再発検知。probe は ~10 token/回。
echo "$(date): ===== litellm_route_probe start ====="
python3 scripts/litellm_route_probe.py || true
echo "$(date): ===== litellm_route_probe end ====="

exit $RC
