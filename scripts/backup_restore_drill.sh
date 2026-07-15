#!/bin/bash
# backup_restore_drill.sh — restic offsite backup から「実際に復元できる」ことを定期検証する。
#
# ★2026-06-08 評価#2 (RPO∞ wall を渡る): backup_offsite.sh で backup 機構はできたが、
#   「restore して実際に戻るか」は一度も検証されていなかった。SRE 鉄則「テストしていない backup は
#   backup ではない」。本 drill が restore を実証して初めて RPO∞ wall が事実として越えられる。
#
# やること:
#   1. restic snapshots を取得 (repo が読める = 復号鍵が有効 を確認)
#   2. canary ファイル (既定 alignment_history.json) を最新 snapshot から `restic dump` で取り出す
#   3. 取り出した内容が非空 + (JSON なら) parse 可能 を検証 (= 復号+展開が健全)
#   4. 本番 disk の同ファイルと diff (一致 = 完全一致、相違 = backup 後に変更=想定内、で報告)
#   5. 初回成功で LINE 通知 + marker、失敗は即 LINE alert
#
# 実行: Mac Studio で週次 cron (日曜 05:00 等)。手動: bash scripts/backup_restore_drill.sh
#       dry-run (snapshots 確認のみ、dump しない): bash scripts/backup_restore_drill.sh --dry-run
#
# 前提: backup_offsite.sh と同じ restic 設定 (.env の RESTIC_REPOSITORY/PASSWORD + creds)。

set -uo pipefail
cd /Users/brain/brain-agent || { echo "$(date): ❌ repo dir 不在 → skip"; exit 1; }

# cron 最小 PATH で restic/git を解決できるよう 3 点セットを source (CLAUDE.md 1.8)
[ -f scripts/cron_env.sh ] && . scripts/cron_env.sh 2>/dev/null || true
if [ -f .env ]; then set -a; . ./.env; set +a; fi

DRY_RUN=0
[ "${1:-}" = "--dry-run" ] && DRY_RUN=1

# canary = 復元検証に使う小さく安定したファイル (env で上書き可)
CANARY="${BACKUP_DRILL_CANARY:-data/brain/alignment_history.json}"

_alert() {
    local msg="$1" tok="${LINE_CHANNEL_ACCESS_TOKEN:-}" usr="${ALIGNMENT_TARGET_USER:-}"
    [ -z "$tok" ] || [ -z "$usr" ] && return 0
    curl -s -X POST https://api.line.me/v2/bot/message/push \
        -H "Authorization: Bearer $tok" -H "Content-Type: application/json" \
        -d "{\"to\":\"$usr\",\"messages\":[{\"type\":\"text\",\"text\":\"$msg\"}]}" >/dev/null 2>&1 || true
}

# 前提チェック (未設定なら loud-skip = backup 自体が未稼働。drill も skip、cron を赤くしない)
if ! command -v restic >/dev/null 2>&1; then
    echo "$(date): restic 未インストール → restore drill skip"; exit 0
fi
if [ -z "${RESTIC_REPOSITORY:-}" ] || [ -z "${RESTIC_PASSWORD:-}" ]; then
    echo "$(date): RESTIC 未設定 → restore drill skip (backup 自体が未稼働)"; exit 0
fi

echo "$(date): ===== restore drill start (canary=$CANARY) ====="

# 1) snapshots (repo 読込 = 復号鍵有効 の確認)
SNAP_COUNT=$(restic snapshots --json 2>/dev/null | python3 -c "import sys,json;
try: print(len(json.load(sys.stdin)))
except Exception: print(0)" 2>/dev/null || echo 0)
if [ "$SNAP_COUNT" = "0" ]; then
    echo "$(date): ❌ snapshot 0 件 (backup 未実行 or repo 不正)"
    _alert "🚨 [Personal Brain] restore drill: snapshot 0 件。offsite backup が一度も成功していない可能性。backup_offsite.sh を確認。"
    exit 1
fi
echo "$(date): snapshots OK ($SNAP_COUNT 件)"

# ★2026-06-30 fix: 鮮度アサーション。「読めるか」だけ見て「新しいか」を見ていなかったため、
# backup が 6/10〜6/30 凍結 (snapshot 1 件のまま) しても drill は ✅ を出し続け、20 日見逃した。
# 最新 snapshot が STALE_MAX_HOURS (既定 48h、6h cron なので十分余裕) より古ければ FAIL + alert。
STALE_MAX_HOURS="${RESTORE_DRILL_STALE_MAX_HOURS:-48}"
LATEST_AGE_H=$(restic snapshots --json --latest 1 2>/dev/null | python3 -c "
import sys, json, datetime
try:
    s = json.load(sys.stdin)
    t = datetime.datetime.fromisoformat(s[-1]['time'].split('.')[0].replace('Z',''))
    print(int((datetime.datetime.now() - t).total_seconds() // 3600))
except Exception:
    print(99999)" 2>/dev/null || echo 99999)
if [ "$LATEST_AGE_H" -gt "$STALE_MAX_HOURS" ]; then
    echo "$(date): ❌ 最新 snapshot が ${LATEST_AGE_H}h 前 (> ${STALE_MAX_HOURS}h) = backup 凍結疑い"
    _alert "🚨 [Personal Brain] restore drill: 最新 snapshot が ${LATEST_AGE_H}h 前と古い (${SNAP_COUNT} 件で凍結疑い)。offsite backup が止まっている。scripts/backup_offsite.sh のログ確認 (cron PATH/restic)。"
    exit 1
fi
echo "$(date): 鮮度 OK (最新 snapshot ${LATEST_AGE_H}h 前 ≤ ${STALE_MAX_HOURS}h)"

# ★2026-07-02 監査 P1e: secret escrow アサーション。
# restic backup は RESTIC_PASSWORD で暗号化されている。host 全損時、offsite に暗号化 backup が
# あっても復号鍵 (.env の RESTIC_PASSWORD) が host にしか無ければ永久に開けない = backup が実質無効。
# 復号鍵を password manager へ escrow 済か (かつ現行鍵と一致か) を検証する。escrow は海山の手作業
# (Claude は secret を扱わない) → 確認記録は scripts/secret_escrow_confirm.sh が書く。
# restore 機構自体は健全 (steps 1-4) なので exit はしない、が未整備なら毎回 loud alert。
ESCROW_MARK="data/brain/.secret_escrow_confirmed"
ESCROW_SALT="pb-escrow-fingerprint-v1"
ESCROW_STALE_DAYS="${SECRET_ESCROW_STALE_DAYS:-180}"
LIVE_RPW_FP=$(printf '%s' "${ESCROW_SALT}:${RESTIC_PASSWORD}" | shasum -a 256 2>/dev/null | cut -c1-16)
ESCROW_STATE="OK"; MARK_DATE=""
if [ ! -f "$ESCROW_MARK" ]; then
    ESCROW_STATE="未escrow"
else
    MARK_FP=$(python3 -c "import json;print(json.load(open('$ESCROW_MARK')).get('restic_pw_fp',''))" 2>/dev/null || echo "")
    MARK_DATE=$(python3 -c "import json;print(json.load(open('$ESCROW_MARK')).get('confirmed_date',''))" 2>/dev/null || echo "")
    if [ -z "$LIVE_RPW_FP" ] || [ "$MARK_FP" != "$LIVE_RPW_FP" ]; then
        ESCROW_STATE="鍵変更(要再escrow)"
    else
        AGE_DAYS=$(python3 -c "
import datetime
try:
    d = datetime.date.fromisoformat('$MARK_DATE'); print((datetime.date.today()-d).days)
except Exception:
    print(99999)" 2>/dev/null || echo 99999)
        [ "$AGE_DAYS" -gt "$ESCROW_STALE_DAYS" ] && ESCROW_STATE="確認が古い(${AGE_DAYS}日)"
    fi
fi
if [ "$ESCROW_STATE" != "OK" ]; then
    echo "$(date): ⚠️ secret escrow: $ESCROW_STATE"
    _alert "🔑 [Personal Brain] backup 復号鍵の escrow 未整備: ${ESCROW_STATE}。host 全損時に offsite backup を復号できない恐れ。(1) .env と RESTIC_PASSWORD を password manager へ保存 (2) Mac Studio で bash scripts/secret_escrow_confirm.sh を実行し確認を記録。"
else
    echo "$(date): 🔑 secret escrow OK (確認日 $MARK_DATE、鍵一致)"
fi

if [ "$DRY_RUN" = "1" ]; then
    echo "$(date): --dry-run → dump/diff は skip。snapshots 読込のみ検証して終了。"
    exit 0
fi

# 2) canary を最新 snapshot から dump
# ★2026-06-10 (Codex MEDIUM): backup_offsite.sh は `restic backup data/brain` と相対パスで保存するため
# snapshot 内の path も `data/brain/...` 相対。絶対パス ($(pwd)/...) で dump すると常に「snapshot に無い」
# で失敗していた (= restore 検証が一度も成功しない偽陰性)。snapshot と同じ相対 path で指定する。
TMP="$(mktemp -t restore_drill_XXXX)" || { echo "mktemp 失敗"; exit 1; }
trap 'rm -f "$TMP"' EXIT

if ! restic dump latest "$CANARY" > "$TMP" 2>/dev/null; then
    echo "$(date): ❌ restic dump 失敗 ($CANARY が snapshot に無い or 復号失敗)"
    _alert "🚨 [Personal Brain] restore drill 失敗: canary ($CANARY) を最新 snapshot から復元できない。backup の中身か復号鍵を確認。"
    exit 1
fi

# 3) 非空 + (JSON なら) parse 検証 = 復号+展開が健全
BYTES=$(wc -c < "$TMP" | tr -d ' ')
if [ "${BYTES:-0}" -lt 2 ]; then
    echo "$(date): ❌ 復元内容が空 ($BYTES bytes)"
    _alert "🚨 [Personal Brain] restore drill 失敗: 復元したファイルが空。backup 破損の疑い。"
    exit 1
fi
JSON_OK="n/a"
case "$CANARY" in
  *.json)
    if python3 -c "import json,sys; json.load(open('$TMP'))" 2>/dev/null; then JSON_OK="valid"; else
        echo "$(date): ❌ 復元 JSON が parse 不可"
        _alert "🚨 [Personal Brain] restore drill 失敗: 復元 JSON が壊れている ($CANARY)。"
        exit 1
    fi ;;
esac

# 4) 本番 disk と diff (相違は backup 後の変更=想定内、報告のみ)
DIFF_STATE="live不在"
if [ -f "$CANARY" ]; then
    if diff -q "$TMP" "$CANARY" >/dev/null 2>&1; then DIFF_STATE="完全一致"; else DIFF_STATE="相違(backup後に変更=想定内)"; fi
fi
echo "$(date): ✅ restore drill OK — canary 復元 $BYTES bytes / JSON=$JSON_OK / vs live=$DIFF_STATE"

# 5) marker + 初回のみ LINE 通知 (RPO∞ wall を「事実として」越えた瞬間)
date +%Y-%m-%d > data/brain/.backup_restore_drill_last 2>/dev/null || true
if [ ! -f data/brain/.backup_restore_drill_success ]; then
    _alert "✅ [Personal Brain] restore drill 初回成功。backup から実際に復元できることを実証 = RPO∞ wall を越えた (戻せる証拠あり)。以降は週次で静かに検証。"
    touch data/brain/.backup_restore_drill_success 2>/dev/null || true
fi
echo "$(date): ===== restore drill done ====="
