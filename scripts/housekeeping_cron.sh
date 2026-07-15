#!/usr/bin/env bash
# housekeeping_cron.sh — 取り込み済みファイルとログの retention (肥大防止) cron wrapper
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# ★2026-06-10 運用堅牢化: 削除機構が無く無限増殖していた 2 系統を日次で掃除する。
#   (1) data/brain/import/processed/  … file watcher が取り込み後に退避する済みファイル。
#       削除機構ゼロで本番 123MB / 1873 file まで肥大していた (再生成可能な中間物)。
#   (2) data/brain/import/*.meta.json … 取り込みメタ。同様に溜まり続ける。
#   (3) data/brain/*.log + data/brain/clone_improve/*.log … cron 出力ログ。
#       scrape.log 6.7MB / clone_improve/cron.log 15MB 等が肥大 (= ディスク圧迫 + tail 重い)。
#
# 方針 (= 非破壊・冪等):
#   - import/processed と import/*.meta.json は mtime +30 日のものだけ delete
#     (取り込み済み中間物なので 30 日経過後は再生成可能、wiki 本体には影響しない)。
#   - ログは 5MB 超のものだけ「末尾 5MB を残して」自己切詰め (tail -c)。
#     直近ログは保持しつつ古い行だけ落とす。tmp → mv の atomic 置換で
#     途中失敗してもログを空にしない。
#
# cron entry (scripts/cron_install.sh で自動登録):
#   15 4 * * * $REPO_ROOT/scripts/housekeeping_cron.sh >> $LOG_DIR/cron.log 2>&1
#
# 手動実行:
#   bash scripts/housekeeping_cron.sh
#   bash scripts/housekeeping_cron.sh --dry-run   # 何も消さず対象だけ表示
#
# dry-run (cron 登録前の必須確認、CLAUDE.md 1.8):
#   env PATH="/usr/bin:/bin" bash scripts/housekeeping_cron.sh --dry-run
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
set -uo pipefail

# cron 3 点セット (PATH / .env / LITELLM_URL) を source
# shellcheck disable=SC1091
source "$(dirname "$0")/cron_env.sh"

REPO_ROOT="${REPO_ROOT:-/Users/brain/brain-agent}"
cd "$REPO_ROOT" || { echo "ERROR: cd $REPO_ROOT failed"; exit 2; }

DRY_RUN=0
[ "${1:-}" = "--dry-run" ] && DRY_RUN=1

RETENTION_DAYS="${HOUSEKEEPING_RETENTION_DAYS:-30}"
LOG_MAX_BYTES="${HOUSEKEEPING_LOG_MAX_BYTES:-5242880}"   # 5MB
PROCESSED_DIR="$REPO_ROOT/data/brain/import/processed"
IMPORT_DIR="$REPO_ROOT/data/brain/import"

echo "$(date): ===== housekeeping start (dry_run=$DRY_RUN, retention=${RETENTION_DAYS}d, log_max=${LOG_MAX_BYTES}B) ====="

# ─── (1)+(2) import/processed と import/*.meta.json の retention ───
# BSD(find) でも GNU でも動く構文のみ使用 (-mtime +N -delete は両対応)。
if [ -d "$PROCESSED_DIR" ]; then
    N_PROC=$(find "$PROCESSED_DIR" -type f -mtime +"$RETENTION_DAYS" 2>/dev/null | wc -l | tr -d ' ')
    echo "$(date): import/processed: ${N_PROC} file(s) > ${RETENTION_DAYS}d"
    if [ "$DRY_RUN" = "0" ]; then
        find "$PROCESSED_DIR" -type f -mtime +"$RETENTION_DAYS" -delete 2>/dev/null || \
            echo "$(date): ⚠️ processed 削除で一部エラー (non-fatal)"
    fi
else
    echo "$(date): import/processed 不在 → skip"
fi

if [ -d "$IMPORT_DIR" ]; then
    N_META=$(find "$IMPORT_DIR" -maxdepth 1 -name '*.meta.json' -mtime +"$RETENTION_DAYS" 2>/dev/null | wc -l | tr -d ' ')
    echo "$(date): import/*.meta.json: ${N_META} file(s) > ${RETENTION_DAYS}d"
    if [ "$DRY_RUN" = "0" ]; then
        find "$IMPORT_DIR" -maxdepth 1 -name '*.meta.json' -mtime +"$RETENTION_DAYS" -delete 2>/dev/null || \
            echo "$(date): ⚠️ meta.json 削除で一部エラー (non-fatal)"
    fi
else
    echo "$(date): import dir 不在 → skip"
fi

# ─── (2.5) 音声アラインメント録音の retention (★2026-07-04 DA: 明示的な保持方針) ───
# 録音は声クローン訓練/再転写用の資産だが deep-private の最機密 artifact でもある。
# 180日で削除 (それまでに用途は果たせる)。transcript (raw *.md) は対象外 = 永続。
REC_DIR="$REPO_ROOT/data/brain/raw/alignment_voice/recordings"
REC_RETENTION_DAYS="${REC_RETENTION_DAYS:-180}"
if [ -d "$REC_DIR" ]; then
    N_REC=$(find "$REC_DIR" -type f -mtime +"$REC_RETENTION_DAYS" 2>/dev/null | wc -l | tr -d ' ')
    echo "$(date): voice recordings: ${N_REC} file(s) > ${REC_RETENTION_DAYS}d"
    if [ "$DRY_RUN" = "0" ]; then
        find "$REC_DIR" -type f -mtime +"$REC_RETENTION_DAYS" -delete 2>/dev/null || \
            echo "$(date): ⚠️ recordings 削除で一部エラー (non-fatal)"
    fi
fi

# ─── (3) ログの自己切詰め (5MB 超のみ、末尾 5MB を残す) ───
# ファイルサイズ取得は BSD(stat -f) を優先し、無ければ GNU(stat -c)、最後に wc -c。
_filesize() {
    stat -f '%z' "$1" 2>/dev/null || stat -c '%s' "$1" 2>/dev/null || wc -c < "$1" 2>/dev/null | tr -d ' '
}

_truncate_log() {
    local f="$1"
    [ -f "$f" ] || return 0
    local sz
    sz=$(_filesize "$f")
    # 数値でなければ skip (安全側)
    case "$sz" in (''|*[!0-9]*) return 0;; esac
    if [ "$sz" -le "$LOG_MAX_BYTES" ]; then
        return 0
    fi
    echo "$(date): trim ${f} (${sz}B > ${LOG_MAX_BYTES}B → 末尾 ${LOG_MAX_BYTES}B 保持)"
    [ "$DRY_RUN" = "1" ] && return 0
    # atomic 置換: tail を tmp に書き、成功時のみ mv。失敗してもログは無傷。
    if tail -c "$LOG_MAX_BYTES" "$f" > "${f}.hk.tmp" 2>/dev/null; then
        mv "${f}.hk.tmp" "$f" 2>/dev/null || { echo "$(date): ⚠️ ${f} mv 失敗"; rm -f "${f}.hk.tmp" 2>/dev/null; }
    else
        echo "$(date): ⚠️ ${f} tail 失敗、切詰めず保持"
        rm -f "${f}.hk.tmp" 2>/dev/null
    fi
}

# glob が一致しない時に literal '*.log' を掴まないよう nullglob 化 (bash)。
shopt -s nullglob 2>/dev/null || true
for f in "$REPO_ROOT"/data/brain/*.log "$REPO_ROOT"/data/brain/clone_improve/*.log; do
    _truncate_log "$f"
done
shopt -u nullglob 2>/dev/null || true

echo "$(date): ===== housekeeping done ====="
exit 0
