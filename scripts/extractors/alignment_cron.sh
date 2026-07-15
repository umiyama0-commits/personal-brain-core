#!/usr/bin/env bash
# alignment_cron.sh — 月初 1 日 04:30 に alignment_snapshot を実行する wrapper。
#
# 月曜の weekly_batch.sh とは独立に走らせる必要がある:
# 月の 1 日が月曜と一致するとは限らないため。
#
# crontab エントリ:
#   30 4 1 * * bash /Users/brain/brain-agent/scripts/extractors/alignment_cron.sh \
#     >> /Users/brain/brain-agent/data/brain/extractor_state/alignment_snapshot.log 2>&1

set -u

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "${SCRIPT_DIR}/../.." && pwd)"

if [ -f "${REPO_ROOT}/.env" ]; then
  set -a
  . "${REPO_ROOT}/.env"
  set +a
fi

export BRAIN_APP_ROOT="${BRAIN_APP_ROOT:-${REPO_ROOT}}"
export LITELLM_MASTER_KEY="${LITELLM_MASTER_KEY:-}"

# host で動かすので docker hostname を localhost に書き換え
if [ -n "${BRAIN_HOST_LITELLM_URL:-}" ]; then
  export LITELLM_URL="${BRAIN_HOST_LITELLM_URL}"
elif [[ "${LITELLM_URL:-}" == *"litellm:"* ]]; then
  export LITELLM_URL="http://localhost:4000"
fi

PYTHON="${PYTHON:-python3}"

echo ""
echo "════════════════════════════════════════════════"
echo "  Alignment snapshot (monthly auto)"
echo "  started:  $(date '+%Y-%m-%d %H:%M:%S %Z')"
echo "  app_root: ${BRAIN_APP_ROOT}"
echo "  litellm:  ${LITELLM_URL}"
echo "════════════════════════════════════════════════"

"${PYTHON}" "${SCRIPT_DIR}/alignment_snapshot.py" --apply --note "自動生成 (cron)"
rc=$?

echo ""
echo "  finished: $(date '+%Y-%m-%d %H:%M:%S %Z')  rc=${rc}"
echo "════════════════════════════════════════════════"
exit ${rc}
