#!/usr/bin/env bash
# scripts/cron_env.sh — 全 cron スクリプト共通の環境セットアップ
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# ★2026-05-19 作成。今セッションで docker-PATH バグが 3 回再発した
# (sales_accuracy_check / sales_data_health / auto_deploy)。
# host cron は最小 PATH (/usr/bin:/bin) で起動し、Docker Desktop /
# Homebrew の bin が PATH 外 → docker / 各種 CLI が "command not found"。
# 新しい cron スクリプトを足す時は必ず冒頭でこれを source すること:
#
#   source "$(dirname "$0")/cron_env.sh"   # scripts/ 配下から
#   source /Users/brain/brain-agent/scripts/cron_env.sh  # 絶対パス
#
# これだけで PATH / .env / LITELLM_URL の cron 3 点セットが揃う。
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

# 1) PATH: Docker Desktop / Homebrew(Intel/AppleSilicon) の bin を前方追加
export PATH="/usr/local/bin:/opt/homebrew/bin:/Applications/Docker.app/Contents/Resources/bin:$PATH"

# 2) .env を source (cron は親 shell の env を継承しないので必須)
_BRAIN_ROOT="/Users/brain/brain-agent"
if [ -f "$_BRAIN_ROOT/.env" ]; then
    set -a
    # shellcheck disable=SC1091
    . "$_BRAIN_ROOT/.env"
    set +a
fi

# 3) LITELLM_URL: docker 内 hostname を host から叩く時は localhost に
if [ "${LITELLM_URL:-}" = "http://litellm:4000" ] || \
   [[ "${LITELLM_URL:-}" == *"litellm:"* ]]; then
    export LITELLM_URL="http://localhost:4000"
fi

# 4) BRAIN_APP_ROOT: clone_improve_lib / clone_ab_test / knowledge_graph 等が
# Path(os.getenv("BRAIN_APP_ROOT", "/app")) で読む。container 内は "/app" だが、
# host cron では "/app" が存在せず silent failure を引き起こす (= 2026-05-27
# 海山指示 「全社員のうみやまAI 利用状況はダッシュボードに反映されてる?」 で
# 5/26-27 の daily metrics 生成失敗が判明、root cause 修正)。
# host cron 経由実行時は brain-agent repo root を指す。
export BRAIN_APP_ROOT="$_BRAIN_ROOT"

# 5) BRAIN_ROOT: bot_events / clone_history / clone_memory が読む data root。
# ★2026-06-07 (エージェント評価 C1): 未 export だと host cron で bot_events が既定 /app/data/brain を
# 読み event 空振り → 監視 (uptime_monitor/monitor_daily/cost_summary) が盲目化。.env に値が
# あればそれを尊重、無ければ repo の data/brain を指す (= container が書く bind mount 元と同一)。
export BRAIN_ROOT="${BRAIN_ROOT:-$_BRAIN_ROOT/data/brain}"
