#!/bin/bash
# untrack_conversations.sh — scraped 会話 (line_/gmail_/gcal_) を git index から外す。
#
# ★2026-06-08 評価 Security: 第三者 PII (社員/取引先の会話) を GitHub から外す。
#   詳細: docs/decisions/2026-06-08-raw-notes-conversations-untrack.md
#
# ★★ 必ず Mac Studio で実行すること ★★
#   MacBook で実行 → push すると auto_deploy が Mac Studio に pull した時に working-tree の
#   会話ファイルを削除する (Mac Studio の会話が消える)。Mac Studio で実行すれば、git rm --cached は
#   そちらの disk にファイルを残すため bot は影響を受けない。
#
# 前提:
#   1. restic backup が初回成功済 (会話が暗号化 backup に入っている)。
#   2. 念のため safety copy (本 script が自動で ~/brain_safety_backups/ に作る = 永続)。
#      また「tracked だが disk 不在」を検出したら消失防止で中断する。
#
# 実行:
#   bash scripts/untrack_conversations.sh           # 確認 + commit (push はしない)
#   bash scripts/untrack_conversations.sh --force    # restic 未確認でも続行

set -uo pipefail
cd /Users/brain/brain-agent || { echo "❌ repo 不在 (Mac Studio で実行?)"; exit 1; }

# 安全確認: restic backup の初回成功マーク
if [ ! -f data/brain/.backup_offsite_first_success ]; then
    echo "⚠️ restic backup の初回成功マークが無い (data/brain/.backup_offsite_first_success)。"
    echo "   会話の暗号化 backup が未確立の可能性。先に backup_offsite.sh を成功させてください。"
    if [ "${1:-}" != "--force" ]; then
        echo "   それでも続行するなら: bash $0 --force"
        exit 1
    fi
    echo "   --force 指定 → 続行 (自己責任)"
fi

PATTERNS=(
    "data/brain/raw/notes/lineworks_*"
    "data/brain/raw/notes/line_*"
    "data/brain/raw/notes/gmail_*"
    "data/brain/raw/notes/gcal_*"
)

N=$(git ls-files "${PATTERNS[@]}" 2>/dev/null | grep -c .)
echo "untrack 対象 (lineworks_/line_/gmail_/gcal_): $N 件"
if [ "$N" = "0" ]; then
    echo "対象なし (既に untrack 済?) → 終了"
    exit 0
fi

# ★DA 欠陥3: tracked だが disk 不在 (= git-only = restic にも無い) を検出。
# あれば untrack で真に消失するため中断 (MacBook 固有 import 等の保護)。
MISSING=$(git ls-files "${PATTERNS[@]}" | while IFS= read -r f; do [ -f "$f" ] || echo "$f"; done)
if [ -n "$MISSING" ]; then
    echo "⚠️ tracked だが disk に存在しないファイルがある (untrack すると消失):"
    echo "$MISSING" | head -20
    echo "   → restic にも無い git-only データ。海山確認まで中断。"
    exit 1
fi

# 二重安全網: safety copy (削除事故に備える)。
# ★DA 欠陥2: /tmp は macOS で再起動/定期 purge → 揮発。永続パスへ。
SAFETY_DIR="$HOME/brain_safety_backups"
SAFETY="$SAFETY_DIR/notes_$(date +%Y%m%d_%H%M%S)"
mkdir -p "$SAFETY_DIR" 2>/dev/null
if cp -a data/brain/raw/notes "$SAFETY" 2>/dev/null; then
    echo "safety copy 作成: $SAFETY (永続パス、要手動削除)"
else
    echo "⚠️ safety copy 失敗 (容量?) → 中断"; exit 1
fi

# index から外す (disk は残す)。日本語ファイル名対応で -z + xargs -0。
git ls-files -z "${PATTERNS[@]}" | xargs -0 -r git rm --cached --quiet --
echo "$N 件を git index から外した (working-tree の disk ファイルは残存)。"

if git diff --cached --quiet; then
    echo "staged 変更なし → 終了"
    exit 0
fi

git commit -q -m "chore(privacy): scraped 会話 (line/gmail/gcal) を git 管理外に (第三者PII、ADR 2026-06-08)

Mac Studio で実行。git rm --cached で index から外し disk には残す (bot 挙動不変)。
会話は restic 暗号化 backup + Mac Studio disk に保持。
詳細: docs/decisions/2026-06-08-raw-notes-conversations-untrack.md

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>"

echo ""
echo "✅ commit 完了。内容を確認して問題なければ手動で push してください:"
echo "     git log --stat -1   # 削除 (index から) されたファイルを確認"
echo "     git push origin main"
echo ""
echo "※ push 後、MacBook が pull すると MacBook 側の copy は削除されます (= MacBook は会話を"
echo "   必要としないので無害)。Mac Studio の disk + restic には残ります。"
echo "※ 過去履歴からの完全除去 (filter-repo) は別途: scripts/purge_conversation_history.sh"
echo "   (untrack だけでは過去 commit に 646 件平文残存。脅威軽減は purge 完了後。海山承認必須)"
