#!/bin/bash
# backup_offsite.sh — 一次データを restic で暗号化して offsite (Backblaze B2 / AWS S3) へ。
#
# ★2026-06-08 システム評価 Tier0 #1: clone_history / clone_memory / alignment / 社内会話 等の
#   再生成不能な一次データが offsite backup ゼロ = RPO∞ (Mac Studio SSD 故障で全消失) だった。
#   restic はローカルで暗号化してから送るため、第三者ストレージに平文を出さない
#   (= data/ を GitHub から gitignore した privacy 方針 CLAUDE.md 1.14 と矛盾しない)。
#
# 前提 (海山が一度だけ用意):
#   1. brew install restic
#   2. .env に以下を設定:
#        RESTIC_REPOSITORY   例) b2:my-brain-backup:restic   /  s3:s3.amazonaws.com/my-brain-backup
#        RESTIC_PASSWORD     repo 暗号化パスワード (★これを失うと復元不能。別途厳重保管)
#        # B2 の場合:  B2_ACCOUNT_ID / B2_ACCOUNT_KEY
#        # S3 の場合:  AWS_ACCESS_KEY_ID / AWS_SECRET_ACCESS_KEY
#   restic / env が無ければ loud-skip (= 設定するまで無害、silent break しない)。
#
# 実行: Mac Studio で 6h おき cron (cron_install.sh が登録)。restic は増分+重複排除で 2 回目以降は安価。

set -uo pipefail
cd /Users/brain/brain-agent || { echo "$(date): ❌ repo dir 不在 → skip"; exit 1; }

# ★2026-06-30 fix (§1.8 違反の根治): cron の最小 PATH では /opt/homebrew/bin/restic が
# 解決できず L53 の `command -v restic` が常に失敗 → 「未インストール → skip」で 6/10〜6/30 の
# 20 日間サイレント停止していた (一次 PII の offsite RPO≈20 日)。cron_env.sh が PATH に
# /opt/homebrew/bin を足すので、.env source の前にこれを source する (restore_drill と同じ作法)。
[ -f scripts/cron_env.sh ] && . scripts/cron_env.sh 2>/dev/null || true

# .env source (cron は親 shell の env を継承しない)
if [ -f .env ]; then
    set -a
    # shellcheck disable=SC1091
    . ./.env
    set +a
fi

# ★2026-06-08 評価 SRE: loud-skip を「log 止まり」から「能動 LINE 通知」に昇格。
# 「機構はあるが RPO∞ のまま放置」を 海山が気付けるよう、未設定なら 1 日 1 回 WARN を push。
_alert() {
    local msg="$1"
    local tok="${LINE_CHANNEL_ACCESS_TOKEN:-}" usr="${ALIGNMENT_TARGET_USER:-}"
    [ -z "$tok" ] || [ -z "$usr" ] && return 0
    curl -s -X POST https://api.line.me/v2/bot/message/push \
        -H "Authorization: Bearer $tok" -H "Content-Type: application/json" \
        -d "{\"to\":\"$usr\",\"messages\":[{\"type\":\"text\",\"text\":\"$msg\"}]}" >/dev/null 2>&1 || true
}

_warn_unconfigured_daily() {
    # 未設定 WARN を 1 日 1 回だけ (6h ごとの spam を防ぐ)
    local marker="data/brain/.backup_offsite_warn_date" today
    today=$(date +%Y-%m-%d)
    if [ "$(cat "$marker" 2>/dev/null || true)" != "$today" ]; then
        _alert "⚠️ [Personal Brain] offsite backup 未設定のまま。一次データ (clone_history/memory/alignment/会話) は依然 RPO∞ = Mac Studio SSD 故障で全消失リスク。restic 導入 + .env に RESTIC_REPOSITORY/PASSWORD を設定してください。"
        echo "$today" > "$marker" 2>/dev/null || true
    fi
}

# 前提チェック → 無ければ loud-skip (exit 0、cron を赤くしない) + 日次 WARN で能動可視化
if ! command -v restic >/dev/null 2>&1; then
    echo "$(date): WARNING: restic 未インストール → offsite backup skip (brew install restic)"
    _warn_unconfigured_daily
    exit 0
fi
if [ -z "${RESTIC_REPOSITORY:-}" ] || [ -z "${RESTIC_PASSWORD:-}" ]; then
    echo "$(date): WARNING: RESTIC_REPOSITORY/PASSWORD 未設定 → offsite backup skip (.env 設定要)"
    _warn_unconfigured_daily
    exit 0
fi

echo "$(date): ===== offsite backup start ====="

# repo 未初期化なら init (冪等。既存なら cat config が成功して skip)
if ! restic cat config >/dev/null 2>&1; then
    echo "$(date): restic repo 未初期化 → restic init"
    if ! restic init; then
        echo "$(date): ❌ restic init 失敗 (repo/creds 確認)"
        exit 1
    fi
fi

# 再生成不能な一次データを backup。
#   除外: chroma_data (= wiki から reindex 可能・巨大) / import/processed (scraper temp) /
#         *.lock / *cookies* (session secret は backup しない) / *.log (ノイズ)
restic backup data/brain \
    --exclude chroma_data \
    --exclude 'import/processed' \
    --exclude '*.lock' \
    --exclude '*cookies*' \
    --exclude '*.log' \
    --tag personal-brain 2>&1 | tail -8
RC=${PIPESTATUS[0]}

# 保持ポリシー: 日次7 / 週次4 / 月次6 を残して間引き + prune (storage 肥大防止)
restic forget --keep-daily 7 --keep-weekly 4 --keep-monthly 6 --prune 2>&1 | tail -5 || true

if [ "$RC" = "0" ]; then
    echo "$(date): ===== offsite backup done (OK) ====="
    # 初回成功だけ LINE 通知 (= RPO∞ が実際に解消した瞬間を 海山に知らせる、以降は静か)
    if [ ! -f "data/brain/.backup_offsite_first_success" ]; then
        _alert "✅ [Personal Brain] offsite backup 初回成功。一次データの暗号化 backup が稼働開始 = RPO∞ → ~6h に解消。"
        touch "data/brain/.backup_offsite_first_success" 2>/dev/null || true
    fi
else
    echo "$(date): ===== offsite backup done (backup rc=$RC = 要確認) ====="
    _alert "🚨 [Personal Brain] offsite backup 失敗 (rc=$RC)。restic repo/creds/容量 を確認してください。"
fi
exit "$RC"
