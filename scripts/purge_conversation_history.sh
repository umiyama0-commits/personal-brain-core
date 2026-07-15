#!/bin/bash
# purge_conversation_history.sh — git **履歴**から scraped 会話 (第三者PII) を完全除去する。
#
# ★2026-06-08 評価#3: untrack_conversations.sh は HEAD から外すだけで、過去 commit には 646 件が
#   平文残存し `git log -p` / raw URL で読める。ADR §背景の脅威 (token 漏洩 / fork 流出) は
#   この履歴 purge まで完了して初めて軽減される。
#
# ★★★ DESTRUCTIVE / IRREVERSIBLE — CLAUDE.md §1.3 海山承認必須 ★★★
#   git filter-repo が **全 commit hash を書き換える** → force-push + 全 clone の re-clone が必要。
#   MacBook も Mac Studio も、purge 後は古い履歴と互換性が無くなる (re-clone 必須)。
#
# 前提 (この順序で):
#   1. restic offsite backup が初回成功済 (data/brain/.backup_offsite_first_success)
#      = 会話が暗号化 backup に入っている (disk からも消えても復元可能)
#   2. untrack_conversations.sh 実行済 (HEAD から index 除去済) + push 済
#   3. git filter-repo インストール済 (brew install git-filter-repo / pip install git-filter-repo)
#   4. 念のため repo 全体を別場所に clone/cp (filter-repo は origin remote を消す)
#
# 使い方:
#   bash scripts/purge_conversation_history.sh --dry-run
#       → 履歴に何件の会話ファイルが存在し purge 対象になるか表示 (一切変更しない、安全)
#   bash scripts/purge_conversation_history.sh --execute --i-have-backup-and-approval
#       → 実際に履歴を書き換える (海山承認下でのみ)。実行後の手順を表示。

set -uo pipefail
cd /Users/brain/brain-agent || { echo "❌ repo 不在 (Mac Studio で実行?)"; exit 1; }

# 会話 prefix (untrack/.gitignore と同一。★lineworks_ 必須 = line_ glob では当たらない)
GLOBS=(
    "data/brain/raw/notes/lineworks_*"
    "data/brain/raw/notes/line_*"
    "data/brain/raw/notes/gmail_*"
    "data/brain/raw/notes/gcal_*"
)

# 履歴全体での対象ファイル数を数える (dry-run / 確認用)
_count_in_history() {
    git log --all --pretty=format: --name-only -- "${GLOBS[@]}" 2>/dev/null \
        | grep -E 'data/brain/raw/notes/(lineworks_|line_|gmail_|gcal_)' \
        | sort -u | wc -l | tr -d ' '
}

MODE="${1:-}"

if [ "$MODE" = "--dry-run" ] || [ -z "$MODE" ]; then
    N=$(_count_in_history)
    echo "=== 履歴 purge dry-run ==="
    echo "履歴 (全 branch) に存在する会話ファイル (lineworks_/line_/gmail_/gcal_): ${N} 種"
    echo ""
    echo "これらは現在 git 履歴に平文で残存し、過去 commit から読める状態です。"
    echo "実行するには (★destructive・海山承認下で):"
    echo "  bash $0 --execute --i-have-backup-and-approval"
    echo ""
    echo "実行前チェックリスト:"
    echo "  [ ] restic backup 初回成功 ($([ -f data/brain/.backup_offsite_first_success ] && echo OK || echo 未))"
    TRACKED_N=$(git ls-files "${GLOBS[@]}" 2>/dev/null | wc -l | tr -d ' ')
    echo "  [ ] untrack_conversations.sh 実行 + push 済 ($([ "${TRACKED_N:-0}" = "0" ] && echo 'OK (HEAD に無し)' || echo "✗ まだ ${TRACKED_N} 件 tracked → untrack 要")) "
    echo "  [ ] git filter-repo インストール ($(command -v git-filter-repo >/dev/null 2>&1 && echo OK || echo 未: brew install git-filter-repo))"
    echo "  [ ] repo を別場所に退避 (filter-repo は origin remote を消す)"
    exit 0
fi

if [ "$MODE" != "--execute" ] || [ "${2:-}" != "--i-have-backup-and-approval" ]; then
    echo "❌ 実行には明示フラグが必要 (destructive・§1.3 承認必須):"
    echo "   bash $0 --execute --i-have-backup-and-approval"
    echo "   まず安全な確認: bash $0 --dry-run"
    exit 1
fi

# --- ここから先は履歴を書き換える (irreversible) ---
echo "=== 履歴 purge execute ==="

# ガード 1: restic backup 成功マーク (会話が暗号化 backup に入っている証跡)
if [ ! -f data/brain/.backup_offsite_first_success ]; then
    echo "❌ restic backup 初回成功マークが無い。purge で disk から会話が消えても復元できない恐れ。"
    echo "   先に backup_offsite.sh を成功させ、restore drill も通すこと。中断。"
    exit 1
fi

# ガード 2: git filter-repo の存在
if ! command -v git-filter-repo >/dev/null 2>&1; then
    echo "❌ git-filter-repo 未インストール (brew install git-filter-repo)。中断。"
    exit 1
fi

# ガード 3: clean working tree (未コミット変更があると filter-repo は止まる)
if ! git diff --quiet || ! git diff --cached --quiet; then
    echo "❌ working tree が clean でない。commit/stash してから。中断。"
    exit 1
fi

N=$(_count_in_history)
echo "履歴から ${N} 種の会話ファイルを全 commit から除去します (5 秒後に開始、Ctrl-C で中断)..."
sleep 5

# filter-repo: --invert-paths で「マッチを除去、他は保持」。各 glob を path-glob で指定。
ARGS=()
for g in "${GLOBS[@]}"; do ARGS+=(--path-glob "$g"); done
git filter-repo --force --invert-paths "${ARGS[@]}"
RC=$?

if [ "$RC" != "0" ]; then
    echo "❌ filter-repo 失敗 (rc=$RC)。履歴は中途半端な可能性 → 退避した clone から復旧を。"
    exit 1
fi

echo ""
echo "✅ 履歴から会話ファイルを除去しました。残りの手順 (手動・要確認):"
echo "  1. origin remote 再追加 (filter-repo が消すため):"
echo "       git remote add origin <repo URL>"
echo "  2. 内容確認: git log --all --oneline | head; git log --all --name-only --pretty=format: -- 'data/brain/raw/notes/lineworks_*' | head  # 空であること"
echo "  3. force-push (★これで GitHub の履歴からも消える):"
echo "       git push origin --force --all && git push origin --force --tags"
echo "  4. **MacBook 側は re-clone 必須** (履歴が乖離。pull では不可):"
echo "       mv ~/brain-agent ~/brain-agent.old && git clone <repo URL> ~/brain-agent"
echo "  5. GitHub の cached view / fork / PR に残らないか確認 (必要なら GitHub サポートに blob 失効依頼)"
echo ""
echo "※ disk の会話ファイル自体は残っています (bot は読み続ける)。git 履歴からのみ除去しました。"
