#!/usr/bin/env bash
# 安い介入 sweep を Mac Studio で安全に実行する (★ADR docs/decisions/2026-06-20-connectome-plasticity-memory.md §8 / kill 基準#1)。
#
# 目的: 「連想グラフ無しの安い tuning (n_results × rerank_top_n) で recall@k がどこまで伸びるか」を
#       golden で実測し、生物学的脳由来の連想想起グラフが本当に要るかを事実で判断する。
#
# ⚠️ chromadb を直接 open するため line-bot を一時停止する (CLAUDE.md §1.5 並行アクセス禁止)。
#    → 数分間、公開うみやまAI が止まる。実行は海山の号令で。trap で必ず再起動する。
#
# 使い方 (Mac Studio, brain-agent 直下で):
#   bash scripts/connectome_baseline_sweep.sh
#   # ローカル(MacBook)で golden 構造だけ事前検証するなら chromadb 不要の:
#   python3 scripts/retrieval_eval.py --dry-run
set -euo pipefail
cd "$(dirname "$0")/.."

# .env を source (cron 同様、env を継承しないケースの保険。§3.6)
set -a; . ./.env 2>/dev/null || true; set +a

OUT="${1:-data/brain/alignment/connectome_sweep_$(date +%Y%m%d_%H%M).txt}"

restart_bot() {
  echo "[sweep] line-bot を再起動..."
  docker compose start line-bot 2>/dev/null || docker compose up -d line-bot
}
trap restart_bot EXIT  # 成否に関わらず必ず bot を戻す

echo "[sweep] line-bot 停止 (chromadb 並行アクセス回避 §1.5)..."
docker compose stop line-bot

echo "[sweep] retrieval sweep 実行 (n_results × rerank_top_n、表 + 出力保存)..."
python3 scripts/retrieval_eval.py --sweep | tee "$OUT"

echo "[sweep] 完了。出力: $OUT  (JSON 明細は data/brain/alignment/retrieval_eval_summary.jsonl)"
echo "[sweep] 判定の見方: baseline(n30_top10) と best cheap の recall@10 差 / 残 full_miss が"
echo "        decision(multi-hop 寄り)に偏るか → ADR §6 kill 基準#1 に従って次を決める。"
