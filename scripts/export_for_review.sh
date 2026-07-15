#!/bin/bash
# export_for_review.sh — 社内エンジニア向けレビュー用 tarball を生成
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# 目的:
#   コードレビュー用に data/ (機密) と chroma_data/ を除外したアーカイブを作る。
#   CLAUDE.md は含める (機密マスキング不要、海山判断 2026-05-21)。
#
# 含める:
#   - 全 .py (main.py, brain_wiki.py, privacy_gate.py, scripts/*.py, tests/**.py, etc.)
#   - 全 .sh (scripts/*.sh)
#   - CLAUDE.md (アーキ + 運用設計が書かれた主要 doc)
#   - docs/ (REVIEW 系)
#   - schema/ (json/yaml schema 定義)
#   - requirements.txt / Dockerfile / docker-compose.yml / pyproject.toml /
#     pytest.ini / .pre-commit-config.yaml / .gitignore
#
# 除外:
#   - data/ 配下すべて (wiki/hobbies/decisions 等 tracked サブツリーも含む) ← 社員 / 売上 raw / 修正希望
#   - chroma_data/ (索引データ、rebuildable)
#   - .env*, *.pem, *.token, cookies.json, client_secret*.json, credentials.json
#   - __pycache__, .pytest_cache, .ruff_cache, .mypy_cache, .venv, venv, build/, dist/
#   - .DS_Store, *.swp, .idea, .vscode
#   - .playwright-mcp/ (大量スクリーンショット・ログ)
#   - .git (history も除外する。過去 commit に data/* が混ざってる可能性、
#           かつコードレビューに git history は必須でない)
#
# 使い方:
#   bash scripts/export_for_review.sh                    # personal-brain-review-YYYY-MM-DD.tar.gz を生成
#   bash scripts/export_for_review.sh --out /tmp/x.tgz   # 出力先指定
#   bash scripts/export_for_review.sh --dry-run          # 何を含めるか list だけ
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

set -uo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
TODAY="$(date +%Y-%m-%d)"
OUT_DEFAULT="$REPO_ROOT/personal-brain-review-${TODAY}.tar.gz"
OUT_PATH="$OUT_DEFAULT"
DRY_RUN=0

while [ $# -gt 0 ]; do
    case "$1" in
        --out)
            OUT_PATH="$2"; shift 2 ;;
        --dry-run)
            DRY_RUN=1; shift ;;
        -h|--help)
            sed -n '1,30p' "$0"; exit 0 ;;
        *)
            echo "unknown arg: $1" >&2; exit 1 ;;
    esac
done

cd "$REPO_ROOT"

# 除外 path (tar の --exclude=)
EXCLUDES=(
    "--exclude=./data"
    "--exclude=./chroma_data"
    "--exclude=./.git"
    "--exclude=./.env"
    "--exclude=./.env.*"
    "--exclude=*.pem"
    "--exclude=*.pem.txt"
    "--exclude=*.key"
    "--exclude=credentials.json"
    "--exclude=client_secret*.json"
    "--exclude=token.json"
    "--exclude=*.token"
    "--exclude=*.token.json"
    "--exclude=cookies.json"
    "--exclude=.session"
    "--exclude=__pycache__"
    "--exclude=*.pyc"
    "--exclude=.pytest_cache"
    "--exclude=.ruff_cache"
    "--exclude=.mypy_cache"
    "--exclude=.venv"
    "--exclude=venv"
    "--exclude=env"
    "--exclude=node_modules"
    "--exclude=build"
    "--exclude=dist"
    "--exclude=.DS_Store"
    "--exclude=*.swp"
    "--exclude=.idea"
    "--exclude=.vscode"
    "--exclude=.playwright-mcp"
    "--exclude=.claude"
    "--exclude=*.tar.gz"
    "--exclude=personal-brain-review-*"
)

# 含めるべき主要パス (確認用 list、tar 本体は exclude 方式で全体 archive)
EXPECTED_PATHS=(
    "main.py"
    "brain_wiki.py"
    "privacy_gate.py"
    "brain_commands.py"
    "clone_history.py"
    "clone_feedback.py"
    "clone_learning.py"
    "clone_memory.py"
    "lineworks_bot.py"
    "content_extractor.py"
    "CLAUDE.md"
    "README.md"
    "requirements.txt"
    "Dockerfile"
    "docker-compose.yml"
    "pyproject.toml"
    "pytest.ini"
    ".pre-commit-config.yaml"
    ".gitignore"
    "scripts/"
    "tests/"
    "docs/"
    "static/"
)

echo "[export_for_review] target = $OUT_PATH"
echo ""

# dry-run なら tar の中身 list だけ
if [ "$DRY_RUN" = "1" ]; then
    echo "[dry-run] tar に入る予定の上位 path:"
    tar "${EXCLUDES[@]}" -czf /dev/null -v -C "$REPO_ROOT" . 2>&1 | \
        head -50
    echo ""
    echo "[dry-run] (ファイル数の概算には数十秒かかります、cancel OK)"
    tar "${EXCLUDES[@]}" -czf /dev/null -v -C "$REPO_ROOT" . 2>&1 | wc -l | \
        awk '{print "  total files: " $1}'
    exit 0
fi

# 機密系の存在チェック (含まれないことを確認)
echo "[check] 機密 path が存在しないか念のため確認..."
risky_found=0
for p in ".env" "data/brain/.lineworks_cookies.json" \
         "data/brain/.stapa_cookies.json" \
         "data/brain/.mobile_owndays_cookies.json" \
         "data/brain/.lw_private_key.pem"; do
    if [ -e "$REPO_ROOT/$p" ]; then
        echo "  ⚠️  $p — tar の --exclude で除外される (中身は archive に入らない)"
    fi
done

# 含めるべき path の存在チェック
echo ""
echo "[check] 含めるべき主要 path:"
missing=0
for p in "${EXPECTED_PATHS[@]}"; do
    if [ -e "$REPO_ROOT/$p" ]; then
        echo "  ✓ $p"
    else
        echo "  ✗ $p (見つからない — 作成漏れ?)"
        missing=$((missing + 1))
    fi
done

if [ "$missing" -gt 0 ]; then
    echo ""
    echo "[warn] 含めるべき path が $missing 件 見つからない。続行?"
fi

# tar 生成
echo ""
echo "[tar] generating $OUT_PATH ..."
tar "${EXCLUDES[@]}" -czf "$OUT_PATH" -C "$REPO_ROOT" .

if [ ! -f "$OUT_PATH" ]; then
    echo "❌ tar 生成失敗"
    exit 1
fi

# ★2026-05-23 LEE §3.1: tarball 中身を secret pattern で scan、見つかったら削除 + exit
echo ""
echo "[secret-scan] tarball 中身を secret pattern で grep..."
SECRET_HITS=$(
    tar -xzOf "$OUT_PATH" 2>/dev/null | \
        grep -nE "sk-(ant|proj|litellm)-[a-zA-Z0-9_-]{10,}|Owndays[0-9]{3,}|LOGIN_PASS\s*=\s*\"[^\"]+\"" \
        2>/dev/null | head -20 || true
)
if [ -n "$SECRET_HITS" ]; then
    echo "❌ tarball に secret pattern を検出:"
    echo "$SECRET_HITS"
    echo ""
    echo "  → tarball を削除しました。漏洩前に修正してから再実行してください"
    rm -f "$OUT_PATH"
    exit 1
fi
echo "  ✓ secret pattern 検出なし"

size_mb=$(du -m "$OUT_PATH" | cut -f1)
n_files=$(tar -tzf "$OUT_PATH" | wc -l | tr -d ' ')
echo ""
echo "[done] $OUT_PATH ($size_mb MB, $n_files files)"
echo ""
echo "[next] 共有方法:"
echo "  1. このファイルを Google Drive / Slack / Box などにアップロード"
echo "  2. 社員エンジニアに URL 共有 (private)"
echo "  3. レビュー観点は docs/review/REVIEW_CHECKLIST.md を参照"
echo "  4. アーキ概観は docs/review/ARCHITECTURE.md を参照"
echo "  5. 全体運用ロジックは CLAUDE.md (このファイル自体が設計書)"
echo ""
echo "[verify] tar の中身を覗くには:"
echo "  tar -tzf $OUT_PATH | head -40"
echo "  tar -tzf $OUT_PATH | grep -i 'env\|cookie\|credential' | head -10  # 機密混入チェック"
