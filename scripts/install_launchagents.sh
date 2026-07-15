#!/usr/bin/env bash
# install_launchagents.sh — repo 追跡の LaunchAgent plist を ~/Library/LaunchAgents に
# 設置して load する (idempotent)。
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# ★2026-06-15: crontab の自動登録 (auto_deploy→cron_install) が cron 起動文脈の
# macOS TCC 権限不足で `crontab update failed` になり機能しないことが判明
# (failure-log 2026-06-15)。再起動も生き残る必要のある critical 常駐 (Docker watchdog 等) は
# crontab でなく launchd LaunchAgent で登録する。LaunchAgent は 1 回 load すれば
# 以後ログイン毎に自動ロード = 再起動も生存し、GUI セッション文脈で走る
# (= watchdog の `open -a Docker` 等 GUI 依存操作も動く)。
#
# 使い方: **SSH/対話セッションから** 1 回実行 (cron 文脈からの launchctl bootstrap gui/ は
# Aqua セッション未バインドで失敗するため、ここは手動 or auto_deploy 外で実行する)。
#   ssh mac-studio 'cd /Users/brain/brain-agent && bash scripts/install_launchagents.sh'
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
set -uo pipefail

SRC_DIR="$(cd "$(dirname "$0")/../deploy/launchagents" && pwd)"
DEST="$HOME/Library/LaunchAgents"
UID_N="$(id -u)"
mkdir -p "$DEST"

shopt -s nullglob
found=0
for plist in "$SRC_DIR"/*.plist; do
  found=1
  name="$(basename "$plist")"
  label="${name%.plist}"
  cp -f "$plist" "$DEST/$name"
  # 既存を一旦 bootout してから bootstrap (plist 更新を確実に反映、idempotent)
  launchctl bootout "gui/$UID_N/$label" 2>/dev/null || true
  if launchctl bootstrap "gui/$UID_N" "$DEST/$name" 2>/dev/null; then
    echo "[launchagents] bootstrap OK: $label"
  elif launchctl load -w "$DEST/$name" 2>/dev/null; then
    echo "[launchagents] load(legacy) OK: $label"
  else
    echo "[launchagents] ❌ load 失敗: $label (GUI セッション有無を確認)"
  fi
  launchctl enable "gui/$UID_N/$label" 2>/dev/null || true
done
[ "$found" = 0 ] && { echo "[launchagents] plist が見つからない: $SRC_DIR"; exit 1; }

echo "--- 現在ロード済み com.brain.* ---"
launchctl list 2>/dev/null | grep -i "com.brain" || echo "(none listed)"
