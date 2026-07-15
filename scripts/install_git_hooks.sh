#!/bin/bash
# install_git_hooks.sh — ★2026-07-02 監査 P1f
# 版管理された scripts/git_hooks/* を .git/hooks/ へ設置する (.git/hooks は clone ごとで非追跡)。
# 各 dev マシンで 1 回 + clone 直後に実行。冪等 (再実行で上書き)。
#
# 実行: bash scripts/install_git_hooks.sh
set -uo pipefail
REPO_ROOT="$(git rev-parse --show-toplevel 2>/dev/null)" || { echo "❌ git repo 外"; exit 1; }
SRC="$REPO_ROOT/scripts/git_hooks"
DST="$REPO_ROOT/.git/hooks"

[ -d "$SRC" ] || { echo "❌ $SRC が無い"; exit 1; }
mkdir -p "$DST"

n=0
for hook in "$SRC"/*; do
    [ -f "$hook" ] || continue
    name="$(basename "$hook")"
    cp "$hook" "$DST/$name"
    chmod +x "$DST/$name"
    echo "  installed: .git/hooks/$name"
    n=$((n+1))
done
echo "✅ $n hook(s) を設置。"

if ! command -v gitleaks >/dev/null 2>&1 \
   && [ ! -x /opt/homebrew/bin/gitleaks ] && [ ! -x /usr/local/bin/gitleaks ]; then
    echo "⚠️ gitleaks が未インストール。pre-commit は fail-closed で commit を止めます。"
    echo "   → brew install gitleaks を実行してください。"
fi
