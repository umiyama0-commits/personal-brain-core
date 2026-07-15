#!/bin/bash
# brain-agent 自動デプロイスクリプト
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# 別 PC で git push されたら、メイン Mac でこのスクリプトが pull → 必要なら rebuild。
# cron で 5 分おきに呼ぶ:
#   */5 * * * * /Users/brain/brain-agent/scripts/auto_deploy.sh >> /Users/brain/brain-agent/data/brain/auto_deploy.log 2>&1
#
# 動作:
#  1. git fetch + 変更チェック
#  2. 変更なければ exit (cron は 5 分おきだが何もしない)
#  3. 変更あれば git pull --rebase
#  4. Python / Dockerfile / requirements 変更があれば docker rebuild + recreate
#  5. それ以外 (md / sh のみ) は再起動だけ (= 即反映)

set -euo pipefail

REPO_ROOT="/Users/brain/brain-agent"
cd "$REPO_ROOT"

# ★2026-06-10: eval gate で auto_deploy の実行が ~5min に延びる日があり、5分 cron と
# 重複起動しうる。flock は macOS に無いため mkdir アトミックロックで防ぐ
# (stale lock = 異常終了の残骸は 30 分で掃除)。
_LOCK_DIR="/tmp/brain_auto_deploy.lock"
if [ -d "$_LOCK_DIR" ] && [ -n "$(find "$_LOCK_DIR" -maxdepth 0 -mmin +30 2>/dev/null)" ]; then
    rmdir "$_LOCK_DIR" 2>/dev/null || true
fi
if ! mkdir "$_LOCK_DIR" 2>/dev/null; then
    echo "$(date): auto_deploy が実行中 (lock 有) → skip"
    exit 0
fi
trap 'rmdir "$_LOCK_DIR" 2>/dev/null || true' EXIT

# ★2026-05-19 根本修正: cron の最小 PATH には docker が無く
# `docker compose build` が "command not found" で毎回失敗、bot が
# 数日前コードのまま動いてた (= 「bot が古い情報」の真因)。
# Docker Desktop / Homebrew の bin を PATH 前方に追加して常に解決する。
export PATH="/usr/local/bin:/opt/homebrew/bin:/Applications/Docker.app/Contents/Resources/bin:$PATH"

# .env から AUTO_DEPLOY_ENABLED / LINE token 等を読む
if [ -f .env ]; then
    set -a
    # shellcheck disable=SC1091
    . ./.env
    set +a
fi

# ★2026-05-19: 失敗時に海山へ LINE Push (silent staleness 再発防止)
# ★2026-06-10: JSON body を生文字列連結 ("{\"text\":\"$msg\"}") で組んでいたため、
# commit subject に含まれる " / \ / 改行で JSON が壊れ、LINE API が 400 を返し
# 通知が無音失敗していた (curl は `|| true` で握り潰す)。codex_review.sh と同じく
# python3 の json.dumps で body 全体を機械生成してエスケープを担保する。
# msg/usr は環境変数で渡す (argv だと先頭 - の値で getopt 風事故、改行混入で崩れる)。
_alert() {
    local msg="$1"
    local tok="${LINE_CHANNEL_ACCESS_TOKEN:-}"
    local usr="${ALIGNMENT_TARGET_USER:-}"
    [ -z "$tok" ] || [ -z "$usr" ] && return 0
    local body
    body=$(_ALERT_MSG="$msg" _ALERT_USR="$usr" python3 -c '
import json, os
print(json.dumps({
    "to": os.environ["_ALERT_USR"],
    "messages": [{"type": "text", "text": os.environ["_ALERT_MSG"][:4900]}],
}))' 2>/dev/null) || return 0
    [ -z "$body" ] && return 0
    printf '%s' "$body" | curl -s -X POST https://api.line.me/v2/bot/message/push \
        -H "Authorization: Bearer $tok" \
        -H "Content-Type: application/json" \
        --data @- \
        >/dev/null 2>&1 || true
}

# ★2026-07-05 §1.18 loud-fail: git 同期失敗は従来 log のみ = 2026-07-01〜 161 回の silent 死
# (judge fix 等が 4 日間未デプロイ)。3 回連続で LINE 通知、以降 6h cooldown
# (毎 5 分の同文 alert = alert 疲れも §1.18 の bug なので streak + cooldown で両方潰す)。
_SYNC_FAIL_STATE="$REPO_ROOT/data/brain/auto_deploy_sync_fail.state"
_sync_fail_loud() {
    local detail="$1" now streak last_alert
    now=$(date +%s)
    { read -r streak last_alert < "$_SYNC_FAIL_STATE"; } 2>/dev/null || { streak=0; last_alert=0; }
    streak=$(( ${streak:-0} + 1 )); last_alert=${last_alert:-0}
    if [ "$streak" -ge 3 ] && [ $((now - last_alert)) -ge 21600 ]; then
        _alert "❌ auto_deploy: git 同期が ${streak} 回連続失敗 (${detail})。本番 code が stale のまま。Studio の data/brain/auto_deploy.log を確認して手動 resolve を。"
        last_alert=$now
    fi
    echo "$streak $last_alert" > "$_SYNC_FAIL_STATE"
}
_sync_fail_reset() { rm -f "$_SYNC_FAIL_STATE" 2>/dev/null || true; }

# ★2026-06-08 システム評価 SRE/LLMOps: deploy rollback の土台。big-bang deploy で health NG に
# なっても従来は alert のみ・手動 git revert だった。build 前に現行 image を退避し、health NG 時に
# 前 image へ自動 revert する (downside-floor = 現状の「down+alert」なので失敗しても悪化しない)。
ROLLBACK_FILE="$REPO_ROOT/data/brain/.rollback_image"

_save_rollback_point() {
    # 現行 line-bot container の image ID と image 名を inspect で実取得して退避。
    local rb_id rb_name
    rb_id=$(docker inspect --format='{{.Image}}' line-bot 2>/dev/null || true)
    rb_name=$(docker inspect --format='{{.Config.Image}}' line-bot 2>/dev/null || true)
    if [ -n "$rb_id" ] && [ -n "$rb_name" ]; then
        echo "$rb_id $rb_name" > "$ROLLBACK_FILE" 2>/dev/null || true
        echo "$(date): rollback point 退避: $rb_name ($rb_id)"
    else
        echo "$(date): rollback point 退避 skip (inspect 失敗 = 初回 or container 不在)"
    fi
}

_rollback_line_bot() {
    # 退避した image に戻して recreate。health OK で 0 を返す。
    [ -f "$ROLLBACK_FILE" ] || { echo "$(date): rollback point 無し"; return 1; }
    local rb_id rb_name
    read -r rb_id rb_name < "$ROLLBACK_FILE" || true
    if [ -z "${rb_id:-}" ] || [ -z "${rb_name:-}" ]; then
        echo "$(date): rollback point 不正"; return 1
    fi
    echo "$(date): rollback 実行 → $rb_name ($rb_id)"
    docker tag "$rb_id" "$rb_name" 2>&1 | tail -2 || { echo "$(date): rollback tag 失敗"; return 1; }
    docker compose up -d --force-recreate line-bot 2>&1 | tail -3 || { echo "$(date): rollback recreate 失敗"; return 1; }
    sleep 8
    curl -sf http://localhost:8000/health >/dev/null
}

# 手動 rollback: `auto_deploy.sh --rollback` で前 image に即復帰 (= 評価者の「1 行で戻せる」)
if [ "${1:-}" = "--rollback" ]; then
    echo "$(date): 手動 rollback 要求"
    if _rollback_line_bot; then
        _alert "↩️ auto_deploy: 手動 rollback 成功 (前 image に復帰、health OK)"
        exit 0
    else
        _alert "🚨 auto_deploy: 手動 rollback 失敗。Mac Studio で docker ps / logs 確認を"
        exit 1
    fi
fi

if [ "${AUTO_DEPLOY_ENABLED:-1}" = "0" ]; then
    echo "$(date): AUTO_DEPLOY_ENABLED=0 → skip"
    exit 0
fi

BRANCH="${AUTO_DEPLOY_BRANCH:-main}"

# git 設定
export GIT_TERMINAL_PROMPT=0  # 認証プロンプトを出さない (ssh key / token のみ)

echo "$(date): ===== auto_deploy start (branch=$BRANCH) ====="

# remote が無ければ即 skip (初回 setup 前)
if ! git remote get-url origin >/dev/null 2>&1; then
    echo "$(date): no origin remote yet, skip (setup_github_remote.sh を実行してね)"
    exit 0
fi

# ★2026-05-25 UU 検出 → 即 alert + exit (silent staleness 真因 1 件目)
# 未解決の merge conflict (UU 状態) があると、後段の自動 stash が
# "could not write index / needs merge" で失敗 → pull 打切り → 5 分おきに
# 同じエラーで「===== auto_deploy start =====」だけ出続ける silent halt 化。
# Task C (2026-05-25) で実発生を確認したため、proactive な検出+通知で
# 「動いてるように見えて実は止まってる」状態を即気付けるようにする。
if git ls-files --unmerged 2>/dev/null | grep -q .; then
    UU_FILES=$(git ls-files --unmerged | awk '{print $4}' | sort -u | head -5 | tr '\n' ' ')
    echo "$(date): ❌ unresolved merge conflict (UU) → deploy halt"
    echo "  files: $UU_FILES"
    echo "  resolve: git checkout HEAD -- <file> && git add <file>"
    _alert "auto_deploy UU halt: 未解決の merge 競合で deploy 停止中。手動 resolve 要 [$UU_FILES]"
    exit 2
fi

# fetch して remote の最新を取る (失敗しても次のサイクル待ち、連続失敗は loud 化 §1.18)
if ! git fetch origin "$BRANCH" 2>&1; then
    echo "$(date): git fetch failed, retry next cycle"
    _sync_fail_loud "git fetch"
    exit 0
fi

LOCAL=$(git rev-parse HEAD)
REMOTE=$(git rev-parse "origin/$BRANCH")

if [ "$LOCAL" = "$REMOTE" ]; then
    # 変更なし。★2026-05-19: heartbeat を残す (L4 が「pipeline は健全、
    # ただデプロイ対象が無いだけ」と判定でき、古い失敗が残り続けない)
    echo "$(date): no change (up to date, pipeline healthy)"
    exit 0
fi

echo "$(date): local=$LOCAL remote=$REMOTE → pulling..."

# 何が変わるか先に検出 (rebuild 要否の判定用)
CHANGED_FILES=$(git diff --name-only "$LOCAL..$REMOTE")
echo "$(date): changed files:"
echo "$CHANGED_FILES" | sed 's/^/  /'

# rebuild 必要 = Python / Dockerfile / requirements / docker-compose
REBUILD=0
if echo "$CHANGED_FILES" | grep -qE '\.(py)$|Dockerfile|requirements.*\.txt|docker-compose.*\.ya?ml|pyproject\.toml'; then
    REBUILD=1
fi

# ★2026-05-15 強化: ローカル変更 (scraper / file watcher の wiki 更新) があれば自動 stash
# Mac mini では bot/scraper が wiki を live で更新する。これと MacBook からの push が
# conflict すると pull --rebase が失敗する。stash → pull → stash pop で両立させる。
STASHED=0
if [ -n "$(git status --porcelain)" ]; then
    echo "$(date): ローカル変更検出 → 自動 stash"
    git stash push -u -m "auto_deploy auto-stash $(date +%Y-%m-%d_%H:%M:%S)" 2>&1 | tail -3
    STASHED=1
fi

# pull 相当: 冒頭で fetch 済みの安定 ref (origin/$BRANCH) へ直接 rebase する。
# ★2026-07-05: 旧 `git pull --rebase origin main` は内部でもう一度 fetch し FETCH_HEAD の
# merge 候補解決に依存 → 並行 git 操作 (海山の Studio 併用セッション等) と重なると
# "fatal: Cannot rebase onto multiple branches" で失敗。2026-07-01 16:30 から 161 回
# silent 失敗し、.py 変更が数日デプロイされない実害が出た。rebase origin/$BRANCH は
# FETCH_HEAD 非依存 + fetch の二度打ちも無くなり決定論的。
if ! git rebase "origin/$BRANCH" 2>&1; then
    echo "$(date): ❌ git rebase origin/$BRANCH failed"
    git rebase --abort 2>/dev/null || true
    # stash を戻す (失敗しても継続)
    if [ "$STASHED" = "1" ]; then
        git stash pop 2>&1 | tail -3 || echo "$(date): ⚠️ stash pop も失敗、stash は残ってる"
    fi
    _sync_fail_loud "rebase origin/$BRANCH"
    exit 1
fi
_sync_fail_reset

# stash 戻す (conflict 起きたら stash は残しておく、手動解決)
if [ "$STASHED" = "1" ]; then
    if git stash pop 2>&1 | grep -q "CONFLICT\|conflict"; then
        # ★2026-07-03 cross-check DA-1: 旧実装は checkout -- . + stash drop で
        # **未コミットの手書きコードを永久削除**していた (log は「保持」と言いながら drop =
        # 5/19 作業消失事故と同族の silent 死)。conflict の working tree だけ戻し、
        # stash は保持 + LINE loud 通知で海山/Claude に手動解決を促す (§1.18)。
        echo "$(date): ⚠️ stash pop で conflict → working tree は戻すが stash は保持 (git stash list で回収可)"
        git checkout -- . 2>/dev/null || true
        _alert "⚠️ auto_deploy: stash pop conflict。未コミットのローカル変更が stash に退避されたまま (git stash list で確認、drop していない)。手動で回収・解決を。"
    fi
fi

echo "$(date): pull ok, REBUILD=$REBUILD"

# ★2026-07-10 (Fable5 supervisor 検証 DA blocker): litellm_config.yaml は volume mount =
# container 起動時にしか読まれない。変更を検知したら litellm を restart して反映する
# (旧: 手動 restart 忘れで新 alias が 400 'invalid model name' のまま silent 稼働する穴)。
# 注: §1.3 auto-remediation (uptime monitor) とは別物 — 自 config 変更の deploy 反映で、
# §1.4「compose 配下編集後は反映操作必須」と同じ deploy 機構の一部。
if echo "$CHANGED_FILES" | grep -q '^litellm_config\.yaml$'; then
    echo "$(date): litellm_config.yaml 変更検知 → docker compose restart litellm"
    if docker compose restart litellm 2>&1 | tail -2; then
        echo "$(date): ✅ litellm restarted (config 反映)"
    else
        _alert "⚠️ auto_deploy: litellm_config.yaml 変更を検知したが litellm restart 失敗。手動で docker compose restart litellm を。"
    fi
fi

# ★2026-05-21 追加: style / prompt / brain_wiki 変更検知 (項目 3: prompt diff check)
# rebuild 完了後に clone_prompt_diff_check を撃って、前夜の baseline と AB 比較
STYLE_PROMPT_CHANGED=0
# ★2026-07-02 cross-check DA (推奨#3): ^brain_wiki_helpers/ を追加 — retrieval 直結 (§1.15(b) 相当) の
# 変更が gate/prompt_diff を skip していた唯一の高リスク群。
if echo "$CHANGED_FILES" | grep -qE '^(brain_wiki|brain_commands|main)\.py$|^brain_wiki_helpers/|^data/brain/wiki/style/|^data/brain/wiki/style\.md$|^data/brain/wiki/knowledge/clone-disclosure-policy\.md$'; then
    STYLE_PROMPT_CHANGED=1
fi

# ★2026-05-25 海山指示: Monday Dash aggregator / stores-by-range builder の変更検知 →
# 即 wiki 再生成 (= 火/水 03:00 cron を待たずに新ロジック反映)。
# build script は scrape data に依存するが、push 直後の re-run で過去 data 分は反映可能。
MONDAY_DASH_CHANGED=0
STORES_RANGE_CHANGED=0
if echo "$CHANGED_FILES" | grep -qE '^scripts/build_monday_dash_latest\.py$'; then
    MONDAY_DASH_CHANGED=1
fi
if echo "$CHANGED_FILES" | grep -qE '^scripts/build_stores_by_customer_range\.py$'; then
    STORES_RANGE_CHANGED=1
fi

if [ "$REBUILD" = "1" ]; then
    echo "$(date): docker rebuild..."
    _save_rollback_point   # ★build 前に現行 image を退避 (health NG 時の自動 revert 用)
    if docker compose build line-bot 2>&1 | tail -5; then
        # ★2026-07-02 監査 P1h: deploy 前 smoke gate (warn-first)。新 image で `import main` が通るか
        # recreate 前に検証し、syntax/import 破壊を crash-loop 前に loud 検知する。
        # `run --rm --no-deps` = 別 throwaway container で import のみ (uvicorn/lifespan 非起動 →
        # chromadb 非 open = §1.5 抵触なし、redis 依存も起動しない)。warn = alert のみで recreate は
        # 続行 (既存の health-check rollback が最終防衛)。block 化は warn 実績後の別 gate。
        if ! docker compose run --rm --no-deps -T line-bot python -c "import main" >/tmp/deploy_smoke_main.log 2>&1; then
            echo "$(date): ❌ smoke gate: 新 image で 'import main' 失敗 (warn=recreate続行):"
            tail -12 /tmp/deploy_smoke_main.log | sed 's/^/    /'
            _alert "🚨 deploy smoke 失敗 ($(git log -1 --format='%h' 2>/dev/null)): 新 image で import main がエラー。health-check rollback が起きるか注視。修正 push を。"
        else
            echo "$(date): smoke gate ok (import main)"
        fi
        echo "$(date): docker recreate..."
        docker compose up -d --force-recreate line-bot 2>&1 | tail -3
        sleep 8
        # health check
        if curl -sf http://localhost:8000/health >/dev/null; then
            DEPLOY_INFO="$(git log --oneline -1)"
            echo "$(date): ✅ deploy ok ($DEPLOY_INFO)"
            # ★2026-05-27 海山指示 (= macbook ↔ Mac Studio 同期): deploy ok を即 LINE Push.
            # 両 PC commit が反映された瞬間を 海山が把握、重複実装 / 進捗ズレに早期気付き.
            DEPLOY_AUTHOR="$(git log -1 --format='%an' 2>/dev/null | head -c 20)"
            DEPLOY_HASH="$(git log -1 --format='%h' 2>/dev/null)"
            DEPLOY_SUBJECT="$(git log -1 --format='%s' 2>/dev/null | head -c 80)"
            _alert "📥 deploy ok: ${DEPLOY_HASH} ${DEPLOY_SUBJECT} (author: ${DEPLOY_AUTHOR})"

            # ★2026-06-08 システム評価 LLMOps G1: deploy 後 eval gate。
            # chromadb 1.5 (並行アクセス禁止) のため pre-deploy の一時コンテナ eval は不可
            # (= live bot と二重 open で SIGSEGV) → deploy 後に bot の in-process eval を回し
            # combined_pass_rate が baseline×threshold を割ったら LINE alert。fail-open
            # (eval 障害は無視)。
            # ★2026-06-08 評価#1 (hard-gate wall を渡る): 既定を off→**warn** に昇格。
            #   DEPLOY_EVAL_GATE 未設定なら warn (= 観測モード、deploy は止めず alert + verdict 記録)。
            #   明示的に "off" で opt-out 可。block (= regression 時に前 image へ自動 rollback) は別途、
            #   warn で数週 baseline 蓄積 + 誤検知率を実測してから切替 (eval_gate_verdicts.jsonl で計測)。
            # ★2026-07-02 監査 P2 (deploy-eval-gate-opus-cost-spike): push 毎に 30問 eval (~$5/回、
            # 7/1 は 14 push ≈ $70/日) が走る構造を絞る。①bot 応答に影響し得る変更
            # (STYLE_PROMPT_CHANGED = brain_wiki/brain_commands/main.py/style wiki) の時だけ実行、
            # ②直近 DEPLOY_EVAL_MIN_INTERVAL_H (default 6h) 以内に実行済なら skip。
            # 夜間 03:30 regression + 04:00 eval-baseline が日次の網として継続。
            # DEPLOY_EVAL_GATE_ALWAYS=1 で旧挙動 (毎 push 実行) に戻せる。
            # marker は gitignore 済の clone_improve/ 配下 (DA nit: alignment/ は !除外で untracked ノイズ化)
            GATE_MARKER="$REPO_ROOT/data/brain/clone_improve/.eval_gate_last_run"
            RUN_GATE=1
            if [ "${DEPLOY_EVAL_GATE_ALWAYS:-0}" != "1" ]; then
                if [ "$STYLE_PROMPT_CHANGED" != "1" ]; then
                    RUN_GATE=0
                    echo "$(date): eval gate skip (bot 応答に影響する変更なし)"
                elif [ -f "$GATE_MARKER" ]; then
                    _gate_last=$(stat -f %m "$GATE_MARKER" 2>/dev/null || echo 0)
                    _gate_min_h="${DEPLOY_EVAL_MIN_INTERVAL_H:-6}"
                    # 非数値 env で set -e の算術死 → deploy 後段 (prompt_diff/cron_install) 全skip を防ぐ
                    [[ "$_gate_min_h" =~ ^[0-9]+$ ]] || _gate_min_h=6
                    if [ $(( $(date +%s) - _gate_last )) -lt $(( _gate_min_h * 3600 )) ]; then
                        RUN_GATE=0
                        echo "$(date): eval gate skip (前回実行から ${_gate_min_h}h 未満)"
                    fi
                fi
            fi
            EVAL_GATE_MODE="${DEPLOY_EVAL_GATE:-warn}"
            if [ "${EVAL_GATE_MODE}" != "off" ] && [ "$RUN_GATE" = "1" ]; then
                echo "$(date): deploy eval gate (${EVAL_GATE_MODE})..."
                touch "$GATE_MARKER" 2>/dev/null || true
                # ★2026-06-10: macOS に GNU timeout が無く `timeout 360 ...` が exit 127 →
                # python が一度も実行されず if が偽 → eval gate が regression を誤検知し、
                # verdict も記録されていなかった (CLAUDE.md 1.8: cron PATH で外部バイナリ解決を実証)。
                # timeout → gtimeout → 素通し で解決 (deploy_eval_gate.py 側に内部 timeout 300 あり)。
                # 外側 600 = python 自体のハング保険 (eval poll の内側 --timeout 480 より長く)。
                if command -v timeout >/dev/null 2>&1; then GATE_TO="timeout 600"
                elif command -v gtimeout >/dev/null 2>&1; then GATE_TO="gtimeout 600"
                else GATE_TO=""; fi
                # shellcheck disable=SC2086  # GATE_TO は word-split させたい (空なら素通し)
                REGR_STREAK_F="$REPO_ROOT/data/brain/clone_improve/.eval_regression_streak"
                if ${GATE_TO} python3 scripts/deploy_eval_gate.py \
                        --base-url http://localhost:8000 \
                        --token "${ALIGNMENT_TRIAL_TOKEN:-${VOICE_ALIGN_TOKEN:-}}" \
                        --threshold "${DEPLOY_EVAL_THRESHOLD:-0.9}" \
                        --timeout "${DEPLOY_EVAL_WAIT:-480}" \
                        --mode "${EVAL_GATE_MODE}" \
                        --commit "${DEPLOY_HASH:-}" 2>&1; then
                    echo "$(date): eval gate ok (or inconclusive)"
                    rm -f "$REGR_STREAK_F" 2>/dev/null || true   # 非 regression で streak リセット
                else
                    echo "$(date): ⚠️ eval gate: regression 検知"
                    # ★2026-06-08 評価: DEPLOY_EVAL_GATE=block なら前 image へ自動 rollback。warn は alert のみ。
                    # ★2026-07-10 (世界基準評価 S4b): block を **2 連続 regression** で初めて rollback する
                    #   safety rail 付きに (verdict 77件中 regression 1 = data 薄く、単発の judge false-positive
                    #   で正常 deploy を巻き戻す自傷を防ぐ)。閾値 = DEPLOY_EVAL_BLOCK_STREAK (default 2)。
                    #   default は warn のまま (= 観測継続、海山が block へ flip する時にこの rail が効く)。
                    _rs=$(cat "$REGR_STREAK_F" 2>/dev/null || echo 0); [[ "$_rs" =~ ^[0-9]+$ ]] || _rs=0
                    _rs=$((_rs + 1)); echo "$_rs" > "$REGR_STREAK_F" 2>/dev/null || true
                    _need="${DEPLOY_EVAL_BLOCK_STREAK:-2}"; [[ "$_need" =~ ^[0-9]+$ ]] || _need=2
                    if [ "${EVAL_GATE_MODE}" = "block" ] && [ "$_rs" -ge "$_need" ]; then
                        rm -f "$REGR_STREAK_F" 2>/dev/null || true
                        if _rollback_line_bot; then
                            _alert "↩️ auto_deploy: eval regression ${_rs}連続 → 前 image に自動 rollback 成功 (block mode)。新 commit ($(git log --oneline -1)) 未反映、prompt/retrieval 要確認。"
                        else
                            _alert "🚨 auto_deploy: eval regression ${_rs}連続 → rollback も失敗。劣化 image 稼働の恐れ。Mac Studio 即確認。"
                        fi
                    elif [ "${EVAL_GATE_MODE}" = "block" ]; then
                        _alert "⚠️ auto_deploy: eval regression 検知 (${_rs}/${_need} 連続、block mode)。単発は rollback せず監視。${_need}連続で自動 rollback。($(git log --oneline -1))"
                    else
                        _alert "⚠️ auto_deploy: deploy 後 eval regression 検知 ($(git log --oneline -1))。combined_pass_rate 低下。要確認 (warn mode = alert のみ、block で ${_need}連続 rollback)"
                    fi
                fi
            fi
        else
            echo "$(date): ⚠️ health check failed after rebuild → 自動 rollback 試行"
            # ★2026-06-08 評価: 新 image が health NG = bot down。前 image へ自動 revert
            # (downside-floor = どのみち down なので失敗しても悪化しない)。
            if _rollback_line_bot; then
                _alert "↩️ auto_deploy: rebuild 後 health NG → 前 image に自動 rollback 成功 (bot 復旧)。新 commit ($(git log --oneline -1)) は未反映、原因調査要。"
            else
                _alert "🚨 auto_deploy: rebuild 後 health NG + 自動 rollback も失敗。bot down の恐れ。Mac Studio 即確認: docker ps / docker logs line-bot --tail 100"
            fi
        fi
    else
        echo "$(date): ❌ docker build failed"
        # ★2026-05-19: silent staleness 再発防止。build 失敗を LINE 通知。
        # (今までこれが無く、docker not found で数日気付かなかった)
        _alert "🚨 auto_deploy: docker build 失敗。bot が古いコードのまま稼働中の恐れ。Mac Studio で docker / auto_deploy.log 確認を"
        exit 1
    fi
else
    echo "$(date): no rebuild needed (md/sh changes only)"
    # ★2026-05-27 海山指示: md / sh のみ変更でも LINE Push 通知 (= 進捗同期 用).
    # 両 PC commit を 海山が把握する目的で、rebuild 有無に関わらず deploy event を通知.
    DEPLOY_AUTHOR="$(git log -1 --format='%an' 2>/dev/null | head -c 20)"
    DEPLOY_HASH="$(git log -1 --format='%h' 2>/dev/null)"
    DEPLOY_SUBJECT="$(git log -1 --format='%s' 2>/dev/null | head -c 80)"
    _alert "📥 sync ok (no rebuild): ${DEPLOY_HASH} ${DEPLOY_SUBJECT} (author: ${DEPLOY_AUTHOR})"
fi

# ★2026-05-25: Monday Dash aggregator 変更 → 即 wiki 再生成 (= 火/水 cron を待たない)
if [ "$MONDAY_DASH_CHANGED" = "1" ]; then
    echo "$(date): Monday Dash aggregator changed → wiki 即時再生成"
    if python3 scripts/build_monday_dash_latest.py 2>&1; then
        echo "$(date): ✅ Monday Dash latest wiki 再生成 OK"
    else
        echo "$(date): ⚠️ build_monday_dash_latest failed (non-fatal、cron 火/水 03:00 で再試行)"
    fi
fi

# ★2026-05-25: stores-by-customer-range builder 変更 → 即 wiki 再生成
if [ "$STORES_RANGE_CHANGED" = "1" ]; then
    echo "$(date): stores-by-customer-range builder changed → wiki 即時再生成"
    if python3 scripts/build_stores_by_customer_range.py 2>&1; then
        echo "$(date): ✅ stores-by-range wiki 再生成 OK"
    else
        echo "$(date): ⚠️ build_stores_by_customer_range failed (non-fatal)"
    fi
fi

# ★2026-05-21: style / prompt 変更時に AB 比較 (バックグラウンド実行、deploy 自体は止めない)
if [ "$STYLE_PROMPT_CHANGED" = "1" ]; then
    echo "$(date): style/prompt change detected, scheduling prompt diff check..."
    SHA="$(git rev-parse HEAD)"
    # 5 秒待ってから別プロセスで実行 (auto_deploy 完了を妨げない)
    (
        sleep 5
        cd "$REPO_ROOT" 2>/dev/null || cd /Users/brain/brain-agent
        python3 scripts/clone_prompt_diff_check.py "$SHA" \
            >> "$REPO_ROOT/data/brain/clone_improve/prompt_diff.log" 2>&1 || \
            echo "$(date): prompt_diff_check failed (non-fatal)" \
            >> "$REPO_ROOT/data/brain/clone_improve/prompt_diff.log"
    ) &
    echo "$(date): prompt_diff_check launched in background (PID $!)"
fi

# ★2026-05-20: cron 自己登録 (MacBook 側で crontab 編集できないため、
# 必要 cron 行は scripts/cron_install.sh に定義しておき、auto_deploy が
# 毎 cycle 冪等に crontab を揃える)
if [ -x ./scripts/cron_install.sh ]; then
    # ★2026-06-28: cron 起動文脈は TCC で crontab 書込不可 → 失敗は想定内。実登録は
    #   LaunchAgent com.brain.cron-install (GUI セッション=権限あり) が 30 分毎に担当する。
    # ★2026-07-02 cross-check S3: この文脈の crontab 書込失敗は設計上の想定内 → loud_fail の
    # streak に数えない (CRON_INSTALL_QUIET=1)。数えると「TCC 未付与」の誤報が飛ぶ。
    CRON_INSTALL_QUIET=1 bash ./scripts/cron_install.sh 2>&1 || echo "$(date): cron_install (cron文脈は crontab 書込不可=想定内、LaunchAgent com.brain.cron-install が登録担当)"
fi

echo "$(date): ===== auto_deploy done ====="
