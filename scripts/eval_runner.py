"""eval_runner.py — eval_set_v1 baseline 計測 (★2026-05-24 Plan C v2 Step 6 Tier 2 E)

Strategy reviewer 指摘「Plan C v2 で何 % 行ったか の唯一の答え」を満たす、絶対基準 scorer。
既存 clone_style_regression.py の query_bot / embed / cosine / llm_judge を import 再利用、
二重実装回避。

# 既存との role 分担

| script | gold set | 評価頻度 | 用途 |
|---|---|---|---|
| clone_style_regression.py | response-bank.md 30Q | 03:30 daily | 前日比 regression、Push 警報 |
| clone_response_quality_judge.py | bot 実トラフィック | 30 分ごと | リアルタイム劣化検知 |
| **eval_runner.py (新)** | **eval_set_v1.json 30 件** | **on-demand + 04:00 daily** | **Plan C v2 baseline、version 比較** |

# 設計

- self-eval loop 防止: bot 応答 = CLONE_PUBLIC_PROD_MODEL (本番追随 ★2026-07-02)、judge = bot と別系列 (clone_style_regression.JUDGE_MODEL) で系列分離
- min_acceptance_score 判定: cosine ≥ per-example threshold で pass/fail (= 0.70-0.85 既定義)
- 30 件順次 (= LiteLLM rate limit 尊重、並列せず)、~1.5-2.5 分で 1 run
- cost: 30 件 × clone_respond ~$0.05 + judge ~$0.10 = ~$0.15/run = ~$5/month
- 結果は data/brain/alignment/eval_results_v1_{date}.jsonl に 1 行 1 件 append
- summary は eval_summary_v1.jsonl (= 1 run 1 行) に append、月次 trend 用

# usage

```
python3 scripts/eval_runner.py --version v1                  # 30 件全部
python3 scripts/eval_runner.py --version v1 --sample 5       # 最初の 5 件のみ (= 動作確認)
python3 scripts/eval_runner.py --version v1 --dry-run        # bot 投げず gold 確認のみ
```

# cron

04:00 daily で実行 (= 03:30 regression / 03:45 hallucination / 03:50 monitor の後)。
"""
from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import httpx

logger = logging.getLogger("eval_runner")
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")

JST = timezone(timedelta(hours=9))
APP_ROOT = Path(os.getenv("BRAIN_APP_ROOT", "/app"))
ALIGNMENT_DIR = APP_ROOT / "data" / "brain" / "alignment"
EVAL_SETS = {
    "v1": ALIGNMENT_DIR / "eval_set_v1.json",
}
RESULTS_DIR = ALIGNMENT_DIR / "eval_results"
SUMMARY_LOG = ALIGNMENT_DIR / "eval_summary_v1.jsonl"
# ★2026-06-07 評価: combined_pass (cosine AND judge>=J_MIN) の judge 閾値 (1-10 scale、6 = 及第点)
J_MIN_JUDGE = int(os.getenv("EVAL_JUDGE_MIN", "6"))


# ─── existing helper の再利用 (= 二重実装回避) ────────────────────────────
sys.path.insert(0, str(Path(__file__).resolve().parent))
try:
    from clone_style_regression import query_bot, embed, cosine, llm_judge  # type: ignore
except Exception as e:
    logger.error(f"clone_style_regression import failed: {e} (eval_runner は無効)")
    raise


# ─── eval set 読込 ─────────────────────────────────────────────────────
def load_eval_set(version: str = "v1") -> list[dict]:
    """eval_set_v1.json を読込、status='approved' なら採用。
    rejected は除外、edit / approve はそのまま。"""
    path = EVAL_SETS.get(version)
    if not path or not path.exists():
        raise FileNotFoundError(f"eval set not found: {version} (path: {path})")
    data = json.loads(path.read_text(encoding="utf-8"))
    if data.get("status") != "approved":
        logger.warning(f"eval set v{version} status != approved: {data.get('status')}")
    examples = []
    for ex in data.get("examples", []):
        if ex.get("verdict") == "reject":
            continue
        examples.append(ex)
    return examples


# ─── 1 件 evaluation ──────────────────────────────────────────────────
async def evaluate_one(
    ex: dict,
    http: httpx.AsyncClient,
    bot_model: str = os.getenv("CLONE_PUBLIC_PROD_MODEL", "smart"),
    brain_wiki=None,
) -> dict:
    """1 example の bot 応答 + cosine + judge を計測。

    Args:
        brain_wiki: BrainWiki instance あれば in-process 呼出 (= /api/eval/run 経由、
                    docker exec 不要)。None なら旧 docker exec path (= host cron 想定)。
    """
    t0 = time.time()
    user_q = ex["user"]
    ideal = ex["assistant_ideal"]
    threshold = ex.get("min_acceptance_score", 0.7)

    # 1. bot 応答 取得
    # ★2026-05-24 bug fix: bot 内部 (= /api/eval/run) から docker exec 不可、
    #   brain_wiki インスタンスあれば in-process 呼出に切替。
    if brain_wiki is not None:
        try:
            bot_response = await brain_wiki.clone_respond_public(
                query=user_q, history=[], model=bot_model,
            )
            err = {}
        except Exception as e:
            bot_response = ""
            err = {"kind": "in_process_error", "detail": str(e)[:200]}
    else:
        # host cron 経由 (= 旧 path)
        bot_response, err = await asyncio.to_thread(_query_bot_sync, user_q, bot_model)
    if err:
        return {
            "id": ex["id"],
            "category": ex.get("category", ""),
            "user": user_q,
            "ideal": ideal,
            "bot_response": "",
            "cosine": 0.0,
            "judge_score": 0,
            "judge_reason": f"bot error: {err}",
            "threshold": threshold,
            "pass": False,
            "latency_ms": int((time.time() - t0) * 1000),
            "error": err,
        }

    # 2. cosine
    emb_ideal = await embed(ideal, http)
    emb_bot = await embed(bot_response, http)
    cos = cosine(emb_ideal or [], emb_bot or [])

    # 3. LLM judge (= bot と別系列、clone_style_regression.JUDGE_MODEL に追随 ★2026-07-02)
    judge = await llm_judge(user_q, ideal, bot_response)
    judge_score = int(judge.get("score", 0))

    return {
        "id": ex["id"],
        "category": ex.get("category", ""),
        "user": user_q,
        "ideal": ideal,
        "bot_response": bot_response,
        "cosine": round(cos, 4),
        "judge_score": judge_score,
        "judge_reason": judge.get("reason", ""),
        "threshold": threshold,
        "pass": cos >= threshold,  # cosine ベース pass/fail (trend 継続のため従来通り)
        # ★2026-06-07 評価: cosine 単独だと「似てるが海山らしくない (judge 低)」応答も pass する。
        #   trend 互換のため primary pass=cosine 据置、combined_pass (cosine AND judge>=J_MIN) を別建て。
        "combined_pass": (cos >= threshold) and (judge_score >= J_MIN_JUDGE),
        "latency_ms": int((time.time() - t0) * 1000),
    }


def _query_bot_sync(query: str, model: str) -> tuple[str, dict]:
    """sync wrapper (= clone_style_regression.query_bot は同期 subprocess)。"""
    return asyncio.run(query_bot(query, model=model))


# ─── 全 30 件 run ─────────────────────────────────────────────────────
async def run_all(
    version: str = "v1",
    sample: int | None = None,
    dry_run: bool = False,
    bot_model: str = os.getenv("CLONE_PUBLIC_PROD_MODEL", "smart"),
    brain_wiki=None,
) -> dict:
    """eval set 全件 (or sample 件) を順次評価、結果 + summary を保存。

    Args:
        brain_wiki: BrainWiki instance あれば in-process 呼出 (= /api/eval/run 経由)、
                    None なら docker exec (= host cron 経由)。
    """
    examples = load_eval_set(version)
    if sample:
        examples = examples[:sample]
    logger.info(f"eval_runner: {len(examples)} examples (version={version}, dry_run={dry_run}, in_process={brain_wiki is not None})")

    if dry_run:
        # bot に投げず eval set の content 確認のみ
        return {
            "version": version,
            "dry_run": True,
            "n_examples": len(examples),
            "category_dist": {
                c: sum(1 for e in examples if e.get("category") == c)
                for c in set(e.get("category", "") for e in examples)
            },
        }

    RESULTS_DIR.mkdir(parents=True, exist_ok=True)
    date_str = datetime.now(JST).strftime("%Y-%m-%d")
    results_file = RESULTS_DIR / f"eval_results_{version}_{date_str}.jsonl"

    results = []
    async with httpx.AsyncClient(timeout=60) as http:
        for i, ex in enumerate(examples):
            logger.info(f"  [{i+1}/{len(examples)}] {ex['id']} ({ex.get('category', '')})")
            r = await evaluate_one(ex, http, bot_model=bot_model, brain_wiki=brain_wiki)
            results.append(r)
            # 1 行 append
            with results_file.open("a", encoding="utf-8") as f:
                f.write(json.dumps(r, ensure_ascii=False) + "\n")

    # summary 集計
    n = len(results)
    n_pass = sum(1 for r in results if r.get("pass"))
    n_combined_pass = sum(1 for r in results if r.get("combined_pass"))
    avg_cos = round(sum(r["cosine"] for r in results) / n, 4) if n else 0.0
    avg_judge = round(sum(r["judge_score"] for r in results) / n, 2) if n else 0.0
    by_cat: dict[str, dict] = {}
    for r in results:
        cat = r.get("category", "")
        if cat not in by_cat:
            by_cat[cat] = {"n": 0, "n_pass": 0, "sum_cos": 0.0, "sum_judge": 0}
        by_cat[cat]["n"] += 1
        by_cat[cat]["n_pass"] += int(r.get("pass", False))
        by_cat[cat]["sum_cos"] += r["cosine"]
        by_cat[cat]["sum_judge"] += r["judge_score"]
    cat_summary = {
        c: {
            "n": v["n"],
            "pass_rate": round(v["n_pass"] / v["n"], 3),
            "avg_cosine": round(v["sum_cos"] / v["n"], 4),
            "avg_judge": round(v["sum_judge"] / v["n"], 2),
        } for c, v in by_cat.items()
    }
    # ★2026-06-08 評価 LLMOps G2: どの prompt 版でこの数字が出たかを記録 (因果追跡)
    try:
        from prompt_version import prompt_version as _pv
        _prompt_ver = _pv()
    except Exception:
        _prompt_ver = "unknown"

    summary = {
        "version": version,
        "prompt_version": _prompt_ver,
        "run_at": datetime.now(JST).isoformat(),
        "bot_model": bot_model,
        "n_examples": n,
        "n_pass": n_pass,
        "pass_rate": round(n_pass / n, 3) if n else 0.0,
        "n_combined_pass": n_combined_pass,  # ★cosine AND judge>=J_MIN (海山らしさも gate)
        "combined_pass_rate": round(n_combined_pass / n, 3) if n else 0.0,
        "avg_cosine": avg_cos,
        "avg_judge": avg_judge,
        "by_category": cat_summary,
        "results_file": str(results_file),
    }
    # summary log に追記 (= 月次 trend 用)
    SUMMARY_LOG.parent.mkdir(parents=True, exist_ok=True)
    with SUMMARY_LOG.open("a", encoding="utf-8") as f:
        f.write(json.dumps(summary, ensure_ascii=False) + "\n")

    logger.info(
        f"eval_runner done: pass_rate={summary['pass_rate']}, "
        f"avg_cosine={summary['avg_cosine']}, avg_judge={summary['avg_judge']}"
    )
    return summary


# ─── CLI ─────────────────────────────────────────────────────────────
def main():
    parser = argparse.ArgumentParser(description="eval_set_v1 baseline 計測 (Plan C v2 Step 6)")
    parser.add_argument("--version", default="v1", help="eval set version (default v1)")
    parser.add_argument("--sample", type=int, default=None, help="N 件のみ (動作確認用)")
    parser.add_argument("--dry-run", action="store_true", help="bot 投げず gold 確認のみ")
    parser.add_argument("--bot-model", default=os.getenv("CLONE_PUBLIC_PROD_MODEL", "smart"),
                        help="bot 応答 model (default = CLONE_PUBLIC_PROD_MODEL、本番 clone に追随 ★2026-07-02 監査)")
    parser.add_argument("--json", action="store_true", help="JSON 出力")
    args = parser.parse_args()

    summary = asyncio.run(run_all(
        version=args.version, sample=args.sample,
        dry_run=args.dry_run, bot_model=args.bot_model,
    ))
    if args.json:
        print(json.dumps(summary, ensure_ascii=False, indent=2))
    else:
        print(f"=== eval_set_v{args.version} baseline ===")
        print(f"  run_at: {summary.get('run_at', '')}")
        print(f"  n: {summary.get('n_examples', 0)}, pass: {summary.get('n_pass', 0)}, "
              f"pass_rate: {summary.get('pass_rate', 0)}")
        print(f"  avg_cosine: {summary.get('avg_cosine', 0)}, avg_judge: {summary.get('avg_judge', 0)}")
        print()
        print("by category:")
        for c, v in summary.get("by_category", {}).items():
            print(f"  {c}: n={v['n']}, pass_rate={v['pass_rate']}, "
                  f"cos={v['avg_cosine']}, judge={v['avg_judge']}")


if __name__ == "__main__":
    main()
