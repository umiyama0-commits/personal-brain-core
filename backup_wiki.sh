#!/bin/bash
# Wiki自動バックアップ — 1時間ごとのcronで実行想定
# 変更があれば自動commit、GitHubリモート設定されていればpush
#
# ★2026-05-15: 外側 git (brain-agent ルート) との衝突回避のため、
#   内側の git dir 名を .git → .git_wiki_backup に変更。
#   外側 git は data/brain/ を普通のディレクトリとして扱える。

set -e

BRAIN_DIR="/Users/brain/brain-agent/data/brain"
GIT_DIR_NAME=".git_wiki_backup"
cd "$BRAIN_DIR"

export GIT_DIR="$BRAIN_DIR/$GIT_DIR_NAME"
export GIT_WORK_TREE="$BRAIN_DIR"

# git repo でなければ初期化
if [ ! -d "$GIT_DIR" ]; then
  git init -q
  git -c user.email="brain@local" -c user.name="Brain Auto" commit --allow-empty -q -m "init"
fi

# ウィキ・会話・メモを追加
# ★2026-05-19: メイン repo が data/ を git 管理外にしたため、
#   従来メイン git が持ってた curated dir (alignment/schema/meta/
#   system_improvements) もここで独立バックアップ対象に加える。
#   これらの履歴保全はこの backup_wiki.sh が唯一の担保になる。
git add .gitignore wiki/ raw/conversations/ raw/notes/ \
  alignment/ schema/ meta/ system_improvements/ \
  system_prompt_patches.json alignment_history.json 2>/dev/null || true

# 差分があればcommit
if ! git diff --staged --quiet 2>/dev/null; then
  TS=$(date +"%Y-%m-%d %H:%M")
  git -c user.email="brain@local" -c user.name="Brain Auto" \
    commit -q -m "auto backup $TS"
  echo "$(date): committed changes"

  # リモートがあればpush（失敗しても続行）
  if git remote get-url origin >/dev/null 2>&1; then
    git push -q origin HEAD 2>&1 || echo "$(date): push failed (continuing)"
  fi
else
  echo "$(date): no changes"
fi
