#!/bin/bash
# codex_review.sh — Codex CLI (別系列 GPT-5) で Claude のコードを独立にレビューする。
#
# ★2026-06-08 海山指示: Claude の自己レビューと別系列で定期コードレビューを回したい。
#   前回の世界基準評価で「cross-check pattern は人手 (Claude) 依存・自動化されてない」と指摘。
#   Codex (read-only) を cron で回す = adversarial review の automation gap を埋める。
#
# モード:
#   weekly  - 前回レビュー以降 (or 直近 7 日) の git diff をレビュー (日曜 cron)
#   monthly - コードベース全体を sweep レビュー (1 日 cron、構造的負債・god object 等)
#
# 通知: 指摘 (findings) があった時だけ LINE 要約。0 件なら静か。全文は履歴ファイルに保存。
#
# 認証 (どちらか 1 つで OK):
#   (a) codex login 済み (~/.codex/auth.json、ChatGPT サブスク) ← Mac Studio は設定済 (2026-05-25)
#   (b) .env に CODEX_API_KEY=<OpenAI key> (API 課金)
#   両方無い / codex 未インストールなら loud-skip (= 設定まで無害)。
#   ★2026-06-10: codex-cli 0.137.0 で --ask-for-approval 廃止 (exec は元々非対話) → flag 除去。
#
# 実行: Mac Studio で cron (cron_install.sh が登録)。手動: bash scripts/codex_review.sh weekly

set -uo pipefail
cd /Users/brain/brain-agent || { echo "$(date): ❌ repo 不在 → skip"; exit 1; }

[ -f scripts/cron_env.sh ] && . scripts/cron_env.sh 2>/dev/null || true
if [ -f .env ]; then set -a; . ./.env; set +a; fi

MODE="${1:-weekly}"
MARKER="data/brain/.codex_review_last_commit"
HIST_DIR="data/brain/codex_review"
mkdir -p "$HIST_DIR" 2>/dev/null
STAMP="$(date +%Y%m%d_%H%M%S)"
OUT="$HIST_DIR/${MODE}_${STAMP}.json"
ERRLOG="$HIST_DIR/${MODE}_${STAMP}.err"

# 前提チェック (未設定なら loud-skip)
if ! command -v codex >/dev/null 2>&1; then
    echo "$(date): codex CLI 未インストール → review skip (npm i -g @openai/codex 等)"; exit 0
fi
if [ -z "${CODEX_API_KEY:-}" ] && [ ! -f "$HOME/.codex/auth.json" ]; then
    echo "$(date): 認証なし (CODEX_API_KEY も ~/.codex/auth.json も無し) → review skip (codex login or .env 設定要)"; exit 0
fi

# model は env で上書き可。既定 = gpt-5.6-sol (★2026-07-12 海山指示「クロスチェックを GPT-5.6 Sol で」。
# 2026-07-09 GA、host codex CLI 0.144.1 へ upgrade 済 + `codex exec -m gpt-5.6-sol` 実証 MODEL_OK。
# 別系列原則 (Claude のコードを OpenAI 系で独立レビュー) は維持)。
CODEX_REVIEW_MODEL="${CODEX_REVIEW_MODEL:-gpt-5.6-sol}"
MODEL_ARG="--model ${CODEX_REVIEW_MODEL}"

# ★2026-06-29 root fix: 台湾/SG 空白地分析の生成 HTML/データが commit され weekly diff が 3.6MB 超 →
# codex が 6/14 以降ずっと rc=1 失敗していた (無言)。制約は 2 段階で binding なのは後者:
#   (1) 入力 API 上限 1,048,576 chars (input_too_large)
#   (2) モデルのコンテキスト窓 ('ran out of room in the model's context window') ← より厳しい
# 対策: ①レビュー対象を「コード」に絞り生成物/データ/バイナリを除外、②なお超なら byte 上限で切詰め
# 未レビュー分を明示。コードレビューの目的 (バグ/ロジック/セキュリティ) は元々コードファイルにしか無い。
CODE_PATHS=( '*.py' '*.sh' '*.bash' '*.yaml' '*.yml' '*.toml' '*.cfg' '*.ini' '*.sql' \
             'Dockerfile' 'Dockerfile.*' 'docker-compose*.yml' 'requirements*.txt' )
# 窓に収める cap (~80k tokens 相当、reasoning+出力の余白込み)。通常の小 diff は無切詰、巨大週のみ効く安全弁。
CODEX_MAX_BYTES="${CODEX_MAX_BYTES:-300000}"

COMMON_INSTR="あなたは厳格なコードレビュアー。OWNDAYS CEO の Personal Brain (LINE bot「うみやまAI」+ 売上 retrieval、Python、2 人開発)。バグ・データ破壊・セキュリティ・ロジック誤り・設計リスクを中心に精査し、findings を severity (high=バグ/破壊/セキュリティ, medium=ロジック/設計, low=軽微) 付きで構造化出力。ファイルは絶対に編集しない (read-only)。問題が無ければ findings は空配列に。誇張せず、再現条件と推奨修正を detail に。"

echo "$(date): ===== codex review start (mode=$MODE, model=${CODEX_REVIEW_MODEL:-CLI既定}) ====="

run_codex() {
    # $1 = prompt, stdin = (任意の diff)。read-only / 承認で止まらない / schema 固定出力。
    codex exec \
        --cd "$(pwd)" \
        --sandbox read-only \
        --skip-git-repo-check \
        --output-schema scripts/codex_review_schema.json \
        --output-last-message "$OUT" \
        ${MODEL_ARG} \
        "$1" >/dev/null 2>"$ERRLOG"
}

if [ "$MODE" = "weekly" ]; then
    LAST=$(cat "$MARKER" 2>/dev/null || true)
    if [ -z "$LAST" ] || ! git cat-file -e "${LAST}^{commit}" 2>/dev/null; then
        LAST=$(git rev-list -n1 --before="7 days ago" HEAD 2>/dev/null || true)
    fi
    [ -z "$LAST" ] && LAST=$(git rev-list --max-parents=0 HEAD | tail -1)
    # 生成物/データ/バイナリを除外しコードのみ (入力上限対策、上記 ★root fix)
    DIFF=$(git diff "${LAST}..HEAD" -- "${CODE_PATHS[@]}" 2>/dev/null)
    if [ -z "$DIFF" ]; then
        echo "$(date): 新規コード変更 無し (${LAST}..HEAD、生成物除外後) → review skip"
        echo "$(git rev-parse HEAD)" > "$MARKER" 2>/dev/null || true
        exit 0
    fi
    DIFF_BYTES=$(printf '%s' "$DIFF" | wc -c | tr -d ' ')
    TRUNC_NOTE=""
    if [ "${DIFF_BYTES:-0}" -gt "$CODEX_MAX_BYTES" ]; then
        DIFF=$(printf '%s' "$DIFF" | head -c "$CODEX_MAX_BYTES")
        TRUNC_NOTE="
(注: コード差分が ${DIFF_BYTES} bytes と大きいため先頭 ${CODEX_MAX_BYTES} bytes のみレビュー。残りは未レビュー。)"
        echo "$(date): ⚠️ code diff ${DIFF_BYTES}B > ${CODEX_MAX_BYTES}B → 切詰めてレビュー"
    fi
    echo "$(date): weekly code diff = ${DIFF_BYTES} bytes (生成物/データ除外済)"
    printf '%s' "$DIFF" | run_codex "${COMMON_INSTR}
以下は直近 1 週間の git diff (${LAST}..HEAD、生成物・データ・バイナリは除外しコードのみ) です。変更箇所を中心にレビューしてください。${TRUNC_NOTE}"
    RC=$?
    [ "$RC" = "0" ] && echo "$(git rev-parse HEAD)" > "$MARKER" 2>/dev/null || true
else
    # monthly: agent が repo を自分で探索 (read-only) して全体 sweep
    run_codex "${COMMON_INSTR}
このリポジトリ全体を read-only で精査し、構造的問題・蓄積した技術的負債・god object (main.py / brain_wiki.py が巨大)・潜在バグ・セキュリティを中心に findings を出してください。重点: main.py, brain_wiki.py, routes/, scripts/, services/。"
    RC=$?
fi

echo "$(date): codex exec rc=$RC, out=$OUT"

# 解析 + 指摘ありの時だけ LINE 通知 (海山の選択: findings>0 のみ通知)
OUT="$OUT" MODE="$MODE" ERRLOG="$ERRLOG" RC="$RC" MODEL_LABEL="${CODEX_REVIEW_MODEL}" python3 - <<'PY' 2>/dev/null || echo "$(date): notify step skipped"
import json, os
out, mode, errlog, rc = os.environ["OUT"], os.environ["MODE"], os.environ["ERRLOG"], os.environ.get("RC","?")
model_label = os.environ.get("MODEL_LABEL", "?")

def notify(msg):
    try:
        import sys as _sys
        _sys.path.insert(0, "scripts")  # clone_improve_lib は scripts/ 配下
        from clone_improve_lib import line_push_digest
        line_push_digest(msg, "Codexレビュー"); return
    except Exception:
        pass
    import urllib.request, json as _j
    tok = os.environ.get("LINE_CHANNEL_ACCESS_TOKEN"); usr = os.environ.get("ALIGNMENT_TARGET_USER")
    if not tok or not usr:
        print("notify: LINE 未設定"); return
    body = _j.dumps({"to": usr, "messages": [{"type":"text","text": msg[:4900]}]}).encode()
    req = urllib.request.Request("https://api.line.me/v2/bot/message/push", data=body,
        headers={"Authorization": f"Bearer {tok}", "Content-Type":"application/json"})
    try: urllib.request.urlopen(req, timeout=10)
    except Exception as e: print(f"notify curl failed: {e}")

data = None
try:
    data = json.load(open(out, encoding="utf-8"))
except Exception:
    pass

if data is None:
    # codex は走ったが schema 出力が得られなかった (codex 自体のエラー含む)
    err = ""
    try: err = open(errlog, encoding="utf-8").read()[-400:]
    except Exception: pass
    if rc != "0" or err:
        notify(f"⚠️ Codex review ({mode}) 実行エラー or 出力 parse 不可 (rc={rc})。\n{out} / {errlog} を確認。\n{err}")
    else:
        print("no parseable output, rc=0 → skip notify")
else:
    findings = data.get("findings", []) or []
    summary = (data.get("summary") or "").strip()
    # ★2026-06-20 dedup (監査⑤): 既知 finding を再通知しない (毎週同じ指摘で alert 疲労を防ぐ)
    import hashlib
    seen_path = os.path.join(os.path.dirname(out) or ".", f"codex_seen_{mode}.json")
    try: seen = set(json.load(open(seen_path, encoding="utf-8")))
    except Exception: seen = set()
    def _key(f):
        s = f"{f.get('severity','')}|{f.get('file','')}|{f.get('title','')}"
        return hashlib.sha1(s.encode("utf-8")).hexdigest()[:16]
    fresh = [f for f in findings if _key(f) not in seen]
    repeats = len(findings) - len(fresh)
    if not findings:
        print(f"codex review ({mode}): 指摘 0 件 → 通知しない")
    elif not fresh:
        print(f"codex review ({mode}): {len(findings)} 件すべて既知 → 通知しない")
    else:
        order = {"high":0,"medium":1,"low":2}
        fresh.sort(key=lambda f: order.get(f.get("severity"),3))
        hi = sum(1 for f in fresh if f.get("severity")=="high")
        md = sum(1 for f in fresh if f.get("severity")=="medium")
        lo = sum(1 for f in fresh if f.get("severity")=="low")
        head = f"🔍 Codex review ({mode}, model={model_label}): 新規 {len(fresh)} 件 (High {hi} / Med {md} / Low {lo})"
        if repeats: head += f" / 既知 {repeats} 件は省略"
        lines = [head]
        if summary: lines += ["", summary]
        lines += [""]
        for f in fresh[:8]:
            lines.append(f"[{f.get('severity','?')}] {f.get('file','?')}: {f.get('title','')}")
        if len(fresh) > 8: lines.append(f"… 他 {len(fresh)-8} 件 (全文: {out})")
        notify("\n".join(lines))
        try:
            seen_list = (list(seen) + [_key(f) for f in fresh])[-500:]  # 上限 500 (際限なき肥大防止)
            json.dump(seen_list, open(seen_path, "w", encoding="utf-8"))
        except Exception as e:
            print(f"seen save failed: {e}")
        print(f"codex review ({mode}): 新規 {len(fresh)} 件 → LINE 通知 (既知 {repeats} 省略)")
PY

# 履歴の肥大防止: 各モード直近 20 件だけ残す
ls -1t "$HIST_DIR"/${MODE}_*.json 2>/dev/null | tail -n +21 | xargs -r rm -f
ls -1t "$HIST_DIR"/${MODE}_*.err 2>/dev/null | tail -n +21 | xargs -r rm -f
echo "$(date): ===== codex review done (mode=$MODE) ====="
