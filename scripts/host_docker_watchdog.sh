#!/usr/bin/env bash
# host_docker_watchdog.sh — Docker デーモン健全性 + 公開サイト死活の host 側 watchdog
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# ★2026-06-15 海山指示。docs/failure-log.md 2026-06-15 (Docker VM メモリ枯渇 →
# Apple Virtualization 709% 暴走 → デーモン無応答 → 公開サイト全断、手動発覚) を受けて新設。
#
# 既存 bot_uptime_monitor.py が埋められない 2 つの穴を埋める:
#   (1) daemon wedge 下では bot_uptime の remediation `docker compose restart` 自体が
#       ハングし、通知に到達する前に固まる → ダウンが手動発覚になった。
#   (2) daemon wedge (VM 暴走) は単体コンテナ再起動では直らない → Docker Desktop 再起動が要る。
#
# 設計方針 = 「絶対にハングしない」:
#   - 全 docker 呼び出しを自前 timeout でラップ (macOS に timeout コマンドが無い)。
#   - 検知 → 通知を remediation より先に出し、通知が docker のハングに巻き込まれないようにする。
#
# 判定:
#   A. daemon wedge = `docker info` が DAEMON_TIMEOUT 秒で返らない (同一 run 内 2 連続で確定)
#   B. site down    = 外部 https://brain.example.com/health が非 200 (daemon は健全)
# 対処 (rate-limited、HOST_WATCHDOG_AUTORESTART=0 で opt-out、auto_deploy ロック中は skip):
#   A → Docker Desktop 再起動 (kill → open → daemon 復活待ち → compose up)  [2h に 1 回まで]
#   B → docker compose up -d line-bot                                      [20分に 1 回まで]
# 通知: 検知時・対処後・失敗時に必ず (line_push + 直 LINE API fallback)。
#
# cron: scripts/cron_install.sh の REQUIRED_CRONS で */5 登録。冒頭で cron_env.sh を source。
# dry-run: `bash scripts/host_docker_watchdog.sh --dry-run` で検知のみ (remediation/通知なし)。
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
set -uo pipefail

DRY_RUN=0
[ "${1:-}" = "--dry-run" ] && DRY_RUN=1

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
# shellcheck disable=SC1091
source "$SCRIPT_DIR/cron_env.sh"

REPO="${BRAIN_APP_ROOT:-/Users/brain/brain-agent}"
DATA="$REPO/data/brain"
LOGP="$DATA/host_watchdog.log"
DOCKER="$(command -v docker || echo /usr/local/bin/docker)"
HEALTH_URL="${HOST_WATCHDOG_HEALTH_URL:-https://brain.example.com/health}"

DAEMON_TIMEOUT="${HOST_WATCHDOG_DAEMON_TIMEOUT:-25}"   # 健全なら docker info は <1s。真の wedge のみ超える
AUTORESTART="${HOST_WATCHDOG_AUTORESTART:-1}"           # 0 で自動復旧 off (検知通知のみ)
RESTART_COOLDOWN=7200                                   # Docker Desktop 再起動は 2h に 1 回まで
COMPOSE_COOLDOWN=1200                                   # compose up は 20 分に 1 回まで
DEPLOY_LOCK="/tmp/brain_auto_deploy.lock"

F_RESTART="$DATA/.wd_last_docker_restart"
F_COMPOSE="$DATA/.wd_last_compose"
F_CFD="$DATA/.wd_last_cloudflared"                      # ★2026-06-30 cloudflared kickstart cooldown
CFD_LABEL="${HOST_WATCHDOG_CFD_LABEL:-com.umiyama.cloudflared}"  # tunnel を管理する LaunchAgent

ts() { date "+%Y-%m-%d %H:%M:%S"; }
log() { echo "$(ts) $*" >> "$LOGP" 2>/dev/null; echo "$(ts) $*"; }

# ★2026-07-10 (世界基準評価 #4): 外部 dead-man switch の heartbeat。
# 全監視 (bot_uptime / 本 watchdog / cron) が監視対象の Mac Studio に**同居**しているため、
# ホスト全死 (停電+FileVault / kernel panic / 回線断) は検知も通知もゼロ = 6/15 は手動発覚だった。
# healthchecks.io 等の HEALTHCHECKS_PING_URL に毎 run (EXIT trap) ping し、host/cron が死んで
# ping が途切れると**外部サービス側が**海山へ alert する (= 唯一 Mac Studio と同居しない監視層)。
# URL は .env で海山が設定 (未設定なら無効)。dry-run では ping しない。
_heartbeat() {
  [ "$DRY_RUN" = 1 ] && return 0
  local u="${HEALTHCHECKS_PING_URL:-}"
  [ -n "$u" ] && curl -fsS -m 10 "$u" -o /dev/null 2>/dev/null || true
}
trap _heartbeat EXIT

# 自前 timeout: "$@" を bg 実行し to 秒で SIGKILL。timeout 時 rc=124、それ以外は本来の rc。
run_to() {
  local to="$1"; shift
  "$@" >/dev/null 2>&1 &
  local p=$! waited=0
  while kill -0 "$p" 2>/dev/null; do
    sleep 1; waited=$((waited + 1))
    if [ "$waited" -ge "$to" ]; then
      kill -9 "$p" 2>/dev/null; wait "$p" 2>/dev/null; return 124
    fi
  done
  wait "$p"; return $?
}

notify() {
  [ "$DRY_RUN" = 1 ] && { log "[dry-run] notify: $1"; return; }
  MSG="$1" REPO="$REPO" python3 - <<'PY' 2>/dev/null || log "notify step skipped"
import os
msg = os.environ["MSG"]
try:
    import sys
    sys.path.insert(0, os.path.join(os.environ["REPO"], "scripts"))
    from clone_improve_lib import line_push
    # Docker daemon wedge / 全断 = 配達保証必須 → critical (LW fallback 許可)
    line_push(msg, critical=True)
    raise SystemExit(0)
except SystemExit:
    raise
except Exception:
    pass
import json, urllib.request
tok = os.environ.get("LINE_CHANNEL_ACCESS_TOKEN"); usr = os.environ.get("ALIGNMENT_TARGET_USER")
if not tok or not usr:
    raise SystemExit(0)
body = json.dumps({"to": usr, "messages": [{"type": "text", "text": msg[:4900]}]}).encode()
req = urllib.request.Request("https://api.line.me/v2/bot/message/push", data=body,
    headers={"Authorization": f"Bearer {tok}", "Content-Type": "application/json"})
try:
    urllib.request.urlopen(req, timeout=10)
except Exception:
    pass
PY
}

cooldown_ok() {  # $1=state file, $2=cooldown sec → 経過していれば 0(ok)
  local f="$1" cd="$2" now last
  now="$(date +%s)"
  [ -f "$f" ] || return 0
  last="$(cat "$f" 2>/dev/null || echo 0)"
  [ $((now - last)) -ge "$cd" ]
}
stamp() { date +%s > "$1" 2>/dev/null; }

site_code() { curl -s -o /dev/null -w "%{http_code}" --max-time 15 "$HEALTH_URL" 2>/dev/null || echo 000; }
# ★2026-06-30: tunnel をバイパスした local 死活 (= 外部 down の真因が bot か tunnel かの切り分け用)。
local_code() { curl -s -o /dev/null -w "%{http_code}" --max-time 5 "http://localhost:8000/health" 2>/dev/null || echo 000; }
# cloudflared (tunnel) を launchd 配下で kickstart (-k = 既存を kill して再起動)。KeepAlive 任せでは
# 「プロセス生存だが connection 劣化」が復旧しないため、能動的に張り直す。gui/user 両ドメインを試行。
kickstart_cloudflared() {
  local uid; uid="$(id -u)"
  launchctl kickstart -k "gui/${uid}/${CFD_LABEL}" >/dev/null 2>&1 \
    || launchctl kickstart -k "user/${uid}/${CFD_LABEL}" >/dev/null 2>&1 \
    || { log "kickstart 失敗 (label=${CFD_LABEL} 未ロード?) → bootstrap 試行"; \
         launchctl bootstrap "gui/${uid}" "$HOME/Library/LaunchAgents/${CFD_LABEL}.plist" >/dev/null 2>&1; }
}

restart_docker_desktop() {
  log "Docker Desktop 再起動シーケンス開始"
  osascript -e 'quit app "Docker"' >/dev/null 2>&1 &
  local i
  for i in 1 2 3 4 5 6 7 8; do sleep 2; pgrep -f com.docker.backend >/dev/null 2>&1 || break; done
  if pgrep -f com.docker.backend >/dev/null 2>&1; then
    log "graceful 効かず → force kill"
    pkill -9 -f "com.docker.backend" 2>/dev/null
    pkill -9 -f "Docker Desktop" 2>/dev/null
    sleep 3
  fi
  if ! open -a Docker 2>/dev/null; then
    log "open -a Docker 失敗 (GUI セッション無し?) → 手動介入要"
    notify "🚨 Docker watchdog: Docker 再起動の open に失敗 (GUI ログインセッション無しの可能性)。Mac Studio で手動復旧が必要です。"
    return 1
  fi
  # daemon 復活待ち (最大 ~150s)
  for i in $(seq 1 30); do
    if run_to 8 "$DOCKER" info; then log "daemon 復活 (~$((i * 5))s)"; break; fi
    sleep 5
  done
  if ! run_to 8 "$DOCKER" info; then
    log "再起動後も daemon 無応答"
    notify "🚨 Docker watchdog: Docker 再起動を試みたが daemon が復活しません。Mac Studio で手動確認を。"
    return 1
  fi
  ( cd "$REPO" && run_to 90 "$DOCKER" compose up -d )
  return 0
}

# ━━━━━ メイン ━━━━━
mkdir -p "$DATA" 2>/dev/null

# (A) daemon 健全性 — docker info の rc を区別:
#   0=健全 / 124=自前timeout(=VM wedge) / 1等=daemon停止 / 127=バイナリ未検出(設定エラー)
#   wedge と down はどちらも Docker Desktop 再起動で復旧。127 は再起動しても無駄なので通知のみ。
#   同一 run 内 2 連続で異常を確認してから動く (transient な info 遅延での誤発火を防ぐ)。
run_to "$DAEMON_TIMEOUT" "$DOCKER" info; D1=$?
if [ "$D1" -eq 127 ]; then
  log "docker バイナリ未検出 ($DOCKER) — 設定エラー (再起動では直らない)"
  [ "$DRY_RUN" = 1 ] && { log "[dry-run] docker 未検出 (ローカルMac等では正常)"; exit 3; }
  notify "🚨 Docker watchdog: docker コマンドが見つかりません ($DOCKER)。PATH/設定を要確認。"
  exit 3
fi
if [ "$D1" -ne 0 ]; then
  sleep 3
  run_to "$DAEMON_TIMEOUT" "$DOCKER" info; D2=$?
  if [ "$D2" -ne 0 ] && [ "$D2" -ne 127 ]; then
    KIND="VM wedge(無応答)"; { [ "$D1" -ne 124 ] && [ "$D2" -ne 124 ]; } && KIND="daemon 停止"
    log "DAEMON_BAD 確定 (docker info rc=$D1,$D2 → $KIND)"
    if [ "$DRY_RUN" = 1 ]; then log "[dry-run] ここで Docker Desktop 再起動を行う ($KIND)"; exit 2; fi
    notify "🚨 Docker watchdog: Docker $KIND を検知。公開サイトが落ちている可能性。復旧を試みます。"
    if [ "$AUTORESTART" != 1 ]; then
      log "AUTORESTART=0 → 自動復旧 skip"; notify "Docker watchdog: AUTORESTART=0 のため自動復旧せず。手動対応を。"; exit 2
    fi
    if [ -d "$DEPLOY_LOCK" ] || [ -f "$DEPLOY_LOCK" ]; then
      log "auto_deploy ロック中 → remediation skip"; notify "Docker watchdog: auto_deploy 実行中のため Docker 再起動を見送り。次サイクルで再判定。"; exit 2
    fi
    if ! cooldown_ok "$F_RESTART" "$RESTART_COOLDOWN"; then
      log "Docker 再起動 cooldown 中 (2h 1回) → skip"; notify "🚨 Docker watchdog: daemon 無応答だが直近2h で再起動済み。restart loop 回避のため停止、手動確認を。"; exit 2
    fi
    stamp "$F_RESTART"
    if restart_docker_desktop; then
      sleep 5; code="$(site_code)"
      log "復旧シーケンス完了 site=/health=$code"
      notify "✅ Docker watchdog: Docker 再起動で復旧 (/health=$code)。原因は VM メモリ等、failure-log 参照。"
    else
      log "復旧シーケンス失敗"
    fi
    exit 2
  fi
fi

# (B) daemon は健全 → 公開サイト死活
CODE="$(site_code)"
if [ "$CODE" = "200" ]; then
  log "healthy (daemon ok, /health=200)"
  exit 0
fi

LOCAL="$(local_code)"
log "SITE_DOWN daemon ok だが 外部/health=$CODE (local/health=$LOCAL)"

# ★2026-06-30: 真因切り分け。local が 200 = bot は健全 → 外部だけ down = Cloudflare tunnel(cloudflared)
# 問題。従来は無条件 line-bot 再起動だったが tunnel 起因では無意味 (cloudflared は KeepAlive で生きてるが
# connection 劣化のケース) → cloudflared を kickstart して張り直す。これが「全断するが誰も復旧しない」の根治。
if [ "$LOCAL" = "200" ]; then
  if [ "$DRY_RUN" = 1 ]; then log "[dry-run] local=200 → ここで cloudflared ($CFD_LABEL) を kickstart"; exit 1; fi
  notify "⚠️ Docker watchdog: 公開 /health=$CODE だが local bot は健全 (200) = Cloudflare tunnel 問題。cloudflared を再起動します。"
  if [ "$AUTORESTART" = 1 ] && cooldown_ok "$F_CFD" "$COMPOSE_COOLDOWN"; then
    stamp "$F_CFD"
    kickstart_cloudflared
    sleep 12; CODE_T="$(site_code)"
    log "cloudflared kickstart 後 外部/health=$CODE_T"
    if [ "$CODE_T" = "200" ]; then
      notify "✅ Docker watchdog: cloudflared 再起動で復旧 (外部/health=200)。"
      exit 0
    fi
    notify "🚨 Docker watchdog: cloudflared 再起動しても外部/health=$CODE_T。CF edge 障害 or cloudflared version (現 $(cloudflared --version 2>/dev/null | awk '{print $3}')) 疑い、手動確認を。"
    exit 1
  fi
  log "cloudflared kickstart cooldown 中 or autorestart=off → skip"
  exit 1
fi

# local も非 200 → bot 問題 → 従来の line-bot 再起動
if [ "$DRY_RUN" = 1 ]; then log "[dry-run] local=$LOCAL → ここで docker compose up -d line-bot を行う"; exit 1; fi
notify "⚠️ Docker watchdog: 公開サイト /health=$CODE + local も $LOCAL (Docker daemon は正常)。line-bot を起動し直します。"
if [ "$AUTORESTART" = 1 ] && ! { [ -d "$DEPLOY_LOCK" ] || [ -f "$DEPLOY_LOCK" ]; } && cooldown_ok "$F_COMPOSE" "$COMPOSE_COOLDOWN"; then
  stamp "$F_COMPOSE"
  ( cd "$REPO" && run_to 90 "$DOCKER" compose up -d line-bot )
  sleep 5; CODE2="$(site_code)"
  log "compose up 後 /health=$CODE2"
  notify "Docker watchdog: line-bot 再起動 → /health=$CODE2"
else
  log "remediation skip (autorestart/deploy-lock/cooldown)"
fi
exit 1
