#!/usr/bin/env bash
# weekly_batch.sh — 自己複製基盤 週次バッチ
#
# 04:00 月曜に host cron から呼び出される想定:
#   0 4 * * 1 cd /Users/brain/brain-agent && bash scripts/extractors/weekly_batch.sh >> /Users/brain/brain-agent/data/brain/extractor_state/weekly_batch.log 2>&1
#
# 各 extractor を順に実行し、失敗してもスキップして次に進む。
# 構造化ログ (events.jsonl) は各 extractor が自前で記録する。
# このスクリプトは start/finish の human-readable サマリだけ標準出力に出す。

set -u  # set -e は使わない (1 個失敗しても残りを走らせたい)

# ─── 環境変数 ───────────────────────────────
# このスクリプトは host から呼ばれることを想定。.env を source してから extractor を呼ぶ。
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "${SCRIPT_DIR}/../.." && pwd)"

if [ -f "${REPO_ROOT}/.env" ]; then
  set -a
  . "${REPO_ROOT}/.env"
  set +a
fi

# host で動かすので APP_ROOT は repo root を指す
export BRAIN_APP_ROOT="${BRAIN_APP_ROOT:-${REPO_ROOT}}"
export LITELLM_MASTER_KEY="${LITELLM_MASTER_KEY:-}"

# .env の LITELLM_URL は docker 内 hostname (http://litellm:4000) を指している。
# host cron から走らせる時は localhost に書き換える。
# .env で BRAIN_HOST_LITELLM_URL を別途定義していればそれを優先。
if [ -n "${BRAIN_HOST_LITELLM_URL:-}" ]; then
  export LITELLM_URL="${BRAIN_HOST_LITELLM_URL}"
elif [[ "${LITELLM_URL:-}" == *"litellm:"* ]]; then
  export LITELLM_URL="http://localhost:4000"
fi
echo "[env] LITELLM_URL=${LITELLM_URL}"

PYTHON="${PYTHON:-python3}"
EX_DIR="${REPO_ROOT}/scripts/extractors"

ts() { date "+%Y-%m-%d %H:%M:%S %Z"; }

echo ""
echo "════════════════════════════════════════════════"
echo "  Self-replication weekly batch"
echo "  started:  $(ts)"
echo "  app_root: ${BRAIN_APP_ROOT}"
echo "════════════════════════════════════════════════"

run_step() {
  local label="$1"
  shift
  echo ""
  echo "── [${label}] $(ts) ──"
  echo "+ $*"
  if "$@"; then
    echo "[${label}] ok"
  else
    local rc=$?
    echo "[${label}] FAILED (rc=${rc}) — continuing"
  fi
}

# ─── 1. style ───────────────────────────────
run_step "style"     "${PYTHON}" "${EX_DIR}/style_extractor.py"     --source all --max-new 6

# ─── 2. judgment ────────────────────────────
run_step "judgment"  "${PYTHON}" "${EX_DIR}/judgment_extractor.py"  --source all --max-new 4

# ─── 3. reflex ──────────────────────────────
run_step "reflex"    "${PYTHON}" "${EX_DIR}/reflex_extractor.py"    --max-new 4

# ─── 4. embodiment scan ─────────────────────
# manifest 投入は手動 (audit Q-004 完了後)。週次ではバイナリ侵入チェックだけ走らせる。
run_step "embodiment_scan" "${PYTHON}" "${EX_DIR}/embodiment_indexer.py" --scan-only

# ─── 5. drift detection ────────────────────
run_step "drift"     "${PYTHON}" "${EX_DIR}/drift_detector.py"  --apply --mark-files

# ─── 6. audit ───────────────────────────────
run_step "audit"     "${PYTHON}" "${EX_DIR}/audit_generator.py" --apply

# ─── 7. alignment snapshot (月初 1 日のみ) ──
# 月初 1 日に「本人像スナップショット」を alignment_state.md に追記。
# 「2026-04 の海山像が永続化されてしまう」ことを構造的に防ぐ装置。
DAY_OF_MONTH="$(date +%d)"
if [ "${DAY_OF_MONTH}" = "01" ]; then
  run_step "alignment_snapshot" "${PYTHON}" "${EX_DIR}/alignment_snapshot.py" --apply
else
  echo ""
  echo "── [alignment_snapshot] $(ts) ──"
  echo "[alignment_snapshot] skipped (today is day ${DAY_OF_MONTH}, only runs on day 01)"
fi

# ─── サマリ ─────────────────────────────────
echo ""
echo "════════════════════════════════════════════════"
echo "  finished: $(ts)"
echo "  events:   ${BRAIN_APP_ROOT}/data/brain/extractor_state/events.jsonl"
echo "  metrics:  ${PYTHON} ${EX_DIR}/extractor_metrics.py --since 7d"
echo "════════════════════════════════════════════════"
