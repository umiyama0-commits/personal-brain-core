#!/bin/bash
# brain-agent GitHub remote セットアップ
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# 使い方:
#   1. GitHub.com で private repo "brain-agent" 作成 (空で OK、README なし)
#   2. このスクリプトに repo URL を渡す:
#      ./scripts/setup_github_remote.sh git@github.com:YOUR_USER/brain-agent.git
#   3. 自動で remote 追加 + push まで実行

set -e

REPO_URL="${1:-}"

if [ -z "$REPO_URL" ]; then
    echo "使い方: $0 git@github.com:YOUR_USER/brain-agent.git"
    echo ""
    echo "GitHub で repo 作成済みか確認:"
    echo "  → https://github.com/new (Repository name: brain-agent, Private)"
    exit 1
fi

cd /Users/brain/brain-agent

# 専用 SSH key を使うため、URL を github.com-brain-agent alias に書き換え
ALIAS_URL=$(echo "$REPO_URL" | sed 's|git@github.com:|git@github.com-brain-agent:|')
echo "remote URL (SSH config alias 経由): $ALIAS_URL"

# remote 設定
if git remote get-url origin >/dev/null 2>&1; then
    echo "origin 既存 → URL を更新"
    git remote set-url origin "$ALIAS_URL"
else
    echo "origin 新規追加"
    git remote add origin "$ALIAS_URL"
fi

# 接続テスト
echo ""
echo "=== SSH 接続テスト ==="
ssh -T git@github.com-brain-agent 2>&1 | head -3

# push
echo ""
echo "=== 初回 push ==="
git push -u origin main 2>&1

echo ""
echo "✅ セットアップ完了"
echo ""
echo "次のステップ:"
echo "  1. auto_deploy cron が 5 分おきに動作確認 (log: data/brain/auto_deploy.log)"
echo "  2. 別 PC でセットアップ → SETUP_MULTI_PC.md 参照"
