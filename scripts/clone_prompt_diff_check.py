#!/usr/bin/env python3
"""
clone_prompt_diff_check.py — deploy 前後の応答スタイル diff 検査

auto_deploy.sh が style / prompt / brain_wiki.py 変更を検知した時、
rebuild 後に新しい regression を撃って **前回 regression** と Q ごとに diff 取る。

ワークフロー:
  1. auto_deploy が rebuild 成功 → このスクリプトを起動 (引数: trigger_sha)
  2. 既存の regression/YYYY-MM-DD.json を pre-deploy ベースラインとして読む
     (前夜 03:30 の clone_style_regression 結果)
  3. clone_style_regression を再実行 (post-deploy 用)
     出力: regression/post-deploy-<sha>.json
  4. Q ごとに比較:
     - cosine 差 > 0.15 で 警告
     - judge_score 差 > 1.5 で 警告
     - violations 増加で警告
  5. 劣化 Q ≥ 3 件 で LINE Push 即時通知

使い方:
  python3 scripts/clone_prompt_diff_check.py <trigger_sha>
  python3 scripts/clone_prompt_diff_check.py --dry-run  (Push しない)

exit code:
  0 = 健全 (劣化なし or 軽微)
  1 = 劣化検出 (Push 済)
  2 = baseline 無し (skip)
"""
from __future__ import annotations

import argparse
import asyncio
import json
import logging
import sys
from datetime import datetime, timedelta
from pathlib import Path
from typing import Optional

sys.path.insert(0, str(Path(__file__).resolve().parent))
from clone_improve_lib import line_push, IMPROVE_DIR, JST

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger("clone_prompt_diff_check")

REGRESSION_DIR = IMPROVE_DIR / "regression"

# 劣化判定の閾値
COSINE_DEGRADATION = 0.15
JUDGE_DEGRADATION = 1.5
VIOLATIONS_INCREASE = 2  # 違反が +2 以上で劣化扱い
DEGRADED_Q_PUSH_THRESHOLD = 3  # 3 件以上で Push


def find_latest_pre_deploy() -> Optional[Path]:
    """直近の nightly regression を pre-deploy ベースラインとして探す。"""
    if not REGRESSION_DIR.exists():
        return None
    # YYYY-MM-DD.json (nightly cron 出力) を新しい順
    # post-deploy / pre-deploy / diff- prefix は除外 (baseline 候補外)
    EXCLUDE_PREFIXES = ("post-deploy-", "pre-deploy-", "diff-")
    candidates = sorted(
        [p for p in REGRESSION_DIR.glob("*.json")
         if not any(p.name.startswith(pre) for pre in EXCLUDE_PREFIXES)],
        key=lambda p: p.name,
        reverse=True,
    )
    return candidates[0] if candidates else None


def compare_qs(pre_q: dict, post_q: dict) -> dict:
    """1 Q の pre/post を比較。"""
    pre_cos = pre_q.get("cosine", 0)
    post_cos = post_q.get("cosine", 0)
    pre_judge = float(pre_q.get("judge_score", 0) or 0)
    post_judge = float(post_q.get("judge_score", 0) or 0)
    pre_viol = len(pre_q.get("violations", []) or [])
    post_viol = len(post_q.get("violations", []) or [])

    cos_delta = round(post_cos - pre_cos, 4)
    judge_delta = round(post_judge - pre_judge, 2)
    viol_delta = post_viol - pre_viol

    degraded_reasons = []
    if cos_delta <= -COSINE_DEGRADATION:
        degraded_reasons.append(f"cosine {cos_delta:+.3f}")
    if judge_delta <= -JUDGE_DEGRADATION:
        degraded_reasons.append(f"judge {judge_delta:+.2f}")
    if viol_delta >= VIOLATIONS_INCREASE:
        degraded_reasons.append(f"violations +{viol_delta}")

    return {
        "id": post_q.get("id"),
        "question": (post_q.get("question") or "")[:60],
        "pre_cosine": pre_cos, "post_cosine": post_cos, "cos_delta": cos_delta,
        "pre_judge": pre_judge, "post_judge": post_judge, "judge_delta": judge_delta,
        "pre_violations": pre_viol, "post_violations": post_viol, "viol_delta": viol_delta,
        "degraded_reasons": degraded_reasons,
        "degraded": bool(degraded_reasons),
    }


async def run_post_deploy_regression(trigger_sha: str) -> Path:
    """clone_style_regression を再実行して post-deploy json を生成。"""
    from clone_style_regression import main as regression_main

    logger.info("running post-deploy regression...")
    rc = await regression_main()
    logger.info(f"regression returned rc={rc}")

    # 今日の YYYY-MM-DD.json を post-deploy 用にリネームコピー
    today = datetime.now(JST).strftime("%Y-%m-%d")
    src = REGRESSION_DIR / f"{today}.json"
    if not src.exists():
        raise FileNotFoundError(f"regression output not found: {src}")

    dst = REGRESSION_DIR / f"post-deploy-{trigger_sha[:7]}-{today}.json"
    dst.write_text(src.read_text(encoding="utf-8"), encoding="utf-8")
    logger.info(f"wrote post-deploy snapshot: {dst.name}")
    return dst


def diff_report(pre_path: Path, post_path: Path) -> dict:
    """pre/post の regression json を読み比較。"""
    pre = json.loads(pre_path.read_text(encoding="utf-8"))
    post = json.loads(post_path.read_text(encoding="utf-8"))

    pre_qs = {q["id"]: q for q in pre.get("questions", [])}
    post_qs = {q["id"]: q for q in post.get("questions", [])}

    comparisons = []
    for qid in sorted(set(pre_qs.keys()) | set(post_qs.keys())):
        if qid not in pre_qs or qid not in post_qs:
            continue
        comparisons.append(compare_qs(pre_qs[qid], post_qs[qid]))

    degraded = [c for c in comparisons if c["degraded"]]
    improved = [
        c for c in comparisons
        if c["cos_delta"] >= COSINE_DEGRADATION
        or c["judge_delta"] >= JUDGE_DEGRADATION
        or c["viol_delta"] <= -VIOLATIONS_INCREASE
    ]

    # 集計
    avg_cos_delta = sum(c["cos_delta"] for c in comparisons) / max(1, len(comparisons))
    avg_judge_delta = sum(c["judge_delta"] for c in comparisons) / max(1, len(comparisons))
    avg_viol_delta = sum(c["viol_delta"] for c in comparisons) / max(1, len(comparisons))

    return {
        "pre_path": str(pre_path),
        "post_path": str(post_path),
        "n_compared": len(comparisons),
        "n_degraded": len(degraded),
        "n_improved": len(improved),
        "avg_cosine_delta": round(avg_cos_delta, 4),
        "avg_judge_delta": round(avg_judge_delta, 2),
        "avg_violations_delta": round(avg_viol_delta, 2),
        "degraded_questions": degraded[:10],  # 上位 10 件
        "improved_questions": improved[:5],
    }


async def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("trigger_sha", nargs="?", default="unknown",
                    help="auto_deploy が pull した commit SHA")
    ap.add_argument("--dry-run", action="store_true", help="Push しない")
    ap.add_argument("--baseline", type=Path, help="pre-deploy json を明示指定 (default は最新 nightly)")
    args = ap.parse_args()

    REGRESSION_DIR.mkdir(parents=True, exist_ok=True)

    # baseline 探す
    pre_path = args.baseline or find_latest_pre_deploy()
    if not pre_path or not pre_path.exists():
        logger.warning("baseline regression が見つからない、nightly 初回なら正常。skip。")
        return 2

    # post-deploy regression を撃つ
    try:
        post_path = await run_post_deploy_regression(args.trigger_sha)
    except Exception as e:
        logger.error(f"post-deploy regression failed: {e}")
        # ★2026-07-02 監査 P2/C: 機構故障 (≠劣化検知) は rc=3 で区別し、loud_fail の記録は
        # __main__ に一元化 (cross-check reviewer B1: main 内で False → __main__ で無条件 True の
        # 相殺 streak バグ + ここでの loud_fail 直呼びは未 import で NameError だった)。
        return 3

    # 比較
    report = diff_report(pre_path, post_path)

    # 保存
    today = datetime.now(JST).strftime("%Y-%m-%d")
    diff_path = REGRESSION_DIR / f"diff-{args.trigger_sha[:7]}-{today}.json"
    diff_path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    logger.info(f"wrote diff report: {diff_path}")

    # 劣化があれば LINE Push
    n_deg = report["n_degraded"]
    if n_deg >= DEGRADED_Q_PUSH_THRESHOLD:
        msg_lines = [
            f"📉 prompt diff 検出 ({args.trigger_sha[:7]})",
            f"劣化 Q: {n_deg}/{report['n_compared']}、改善 Q: {report['n_improved']}",
            f"avg cosine: {report['avg_cosine_delta']:+.4f}",
            f"avg judge: {report['avg_judge_delta']:+.2f}",
            f"avg violations: {report['avg_violations_delta']:+.2f}",
            "",
            "劣化 Q 上位 3 件:",
        ]
        for c in report["degraded_questions"][:3]:
            msg_lines.append(f"- {c['id']}: {c['question']}")
            msg_lines.append(f"  reasons: {', '.join(c['degraded_reasons'])}")
        msg_lines.append("")
        msg_lines.append(f"詳細: {diff_path}")

        if args.dry_run:
            print("[DRY RUN]\n" + "\n".join(msg_lines))
        else:
            line_push("\n".join(msg_lines))  # deploy 直後の劣化検知 = 即 revert 可能な actionable、即時維持 (cross-check DA)
        return 1

    logger.info(f"OK: degraded={n_deg} < threshold={DEGRADED_Q_PUSH_THRESHOLD}, no push")
    return 0


if __name__ == "__main__":
    # ★2026-07-02 監査 C (§1.18): 機構の成否を「ここ 1 箇所」で loud_fail に記録する
    # (main 内と二重記録すると False→True 相殺で threshold に届かない: cross-check reviewer B1)。
    # rc の意味: 0=OK / 1=劣化検知 (Push 済 = 機構は動いた) / 2=baseline 無し (正常 skip) /
    # 3=機構故障 (regression 実行不能 = loud 対象)。import/main 前の即死 (旧: /app mkdir OSError で
    # 6/1 から丸ごと死亡) は except で False 記録。
    from clone_improve_lib import loud_fail
    try:
        rc = asyncio.run(main())
    except Exception as e:
        loud_fail("prompt_diff_check", False, f"{type(e).__name__}: {str(e)[:120]}",
                  threshold=3, cooldown_h=24)
        raise
    loud_fail("prompt_diff_check", rc != 3,
              "post-deploy regression 実行不能 (機構故障)" if rc == 3 else "",
              threshold=3, cooldown_h=24)
    sys.exit(rc)
