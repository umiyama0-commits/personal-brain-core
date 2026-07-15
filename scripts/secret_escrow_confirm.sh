#!/bin/bash
# secret_escrow_confirm.sh — ★2026-07-02 監査 P1e
#
# 海山が .env と RESTIC_PASSWORD を password manager (1Password 等) へ保存 (escrow) した後、
# **1 回だけ** Mac Studio で実行する。現行 secret の指紋 (sha256、値そのものは保存しない) と
# 確認日を marker (data/brain/.secret_escrow_confirmed) に記録する。
#
# これ以降 backup_restore_drill.sh が「復号鍵が escrow 済 かつ 現行鍵と一致」を毎回検証し、
# 未整備・鍵変更・確認が古い場合に loud alert する (host 全損時に復号できない = backup 無効 を防ぐ)。
#
# 鍵をローテートしたら escrow し直して本 script を再実行 (指紋が変わり drill が再 escrow を促す)。
#
# 実行: bash scripts/secret_escrow_confirm.sh
set -uo pipefail
cd /Users/brain/brain-agent || { echo "repo dir 不在"; exit 1; }
[ -f scripts/cron_env.sh ] && . scripts/cron_env.sh 2>/dev/null || true
if [ -f .env ]; then set -a; . ./.env; set +a; fi

ESCROW_SALT="pb-escrow-fingerprint-v1"
if [ -z "${RESTIC_PASSWORD:-}" ]; then
    echo "❌ RESTIC_PASSWORD 未設定 (.env)。escrow 対象が無い = backup 未稼働。先に backup_offsite.sh を確認。"
    exit 1
fi

RPW_FP=$(printf '%s' "${ESCROW_SALT}:${RESTIC_PASSWORD}" | shasum -a 256 2>/dev/null | cut -c1-16)
ENV_FP=$(shasum -a 256 .env 2>/dev/null | cut -c1-16)
TODAY=$(date +%Y-%m-%d)

python3 - "$RPW_FP" "$ENV_FP" "$TODAY" <<'PY'
import json, sys
fp_rpw, fp_env, today = sys.argv[1], sys.argv[2], sys.argv[3]
rec = {
    "confirmed_date": today,
    "restic_pw_fp": fp_rpw,           # 指紋のみ (値は非保存)。鍵ローテ検知用
    "env_fp": fp_env,                 # 参考 (.env は頻繁に変わるので drill の hard assert には使わない)
    "note": "海山が password manager へ .env + RESTIC_PASSWORD を escrow 済と確認",
}
with open("data/brain/.secret_escrow_confirmed", "w", encoding="utf-8") as f:
    json.dump(rec, f, ensure_ascii=False, indent=2)
PY

echo "✅ escrow 確認を記録: $TODAY (RESTIC_PASSWORD fp=$RPW_FP)"
echo "   以降 backup_restore_drill.sh が復号鍵の escrow 整合を毎回検証します。"
