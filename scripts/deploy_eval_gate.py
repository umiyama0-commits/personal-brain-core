#!/usr/bin/env python3
"""deploy_eval_gate.py — deploy 後に in-process eval を回し regression を判定する。

★2026-06-08 システム評価 LLMOps G1: 「eval が deploy を一度も gate しない」穴を塞ぐ。

なぜ pre-deploy でなく post-deploy か:
  chromadb 並行アクセス禁止 (CLAUDE.md 1.5)。新 image を一時コンテナで eval すると、稼働中の
  live bot と同じ chromadb を二重 open し SIGSEGV crash loop になる。安全に eval できるのは
  bot の in-process 経路 (/api/eval/run = 既に open 済の brain instance を使う) のみ。よって
  deploy 後に eval を回し、regression なら呼び出し側 (auto_deploy.sh) が alert / rollback する。

fail-open 原則:
  eval 自体のエラー・timeout・baseline 不在は **exit 0** (= deploy を止めない)。誤検知で正常な
  deploy を巻き戻す方が有害。**明確に combined_pass_rate が baseline×threshold を割った時のみ exit 1**。

usage (auto_deploy.sh から DEPLOY_EVAL_GATE=warn|block で呼ぶ):
  python3 scripts/deploy_eval_gate.py --base-url http://localhost:8000 --token "$TOKEN" --threshold 0.9
exit: 0 = ok / inconclusive (fail-open) / 1 = regression
"""
from __future__ import annotations

import argparse
import json
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DEFAULT_SUMMARY = ROOT / "data" / "brain" / "alignment" / "eval_summary_v1.jsonl"
# ★2026-06-08 評価#1: warn 期間の誤検知率を後で実測するため、毎 gate 判定を記録する。
# (warn で N deploy 走らせ、regression 判定のうち実際に劣化だったのが何件か = block 切替の根拠)
VERDICT_LOG = ROOT / "data" / "brain" / "alignment" / "eval_gate_verdicts.jsonl"
_JST = timezone(timedelta(hours=9))


def _log_verdict(verdict, current, baseline, threshold, current_n, current_pass,
                 mode, commit) -> None:
    """gate 判定を jsonl に append (fail-safe = 失敗しても gate を壊さない)。"""
    try:
        floor = round(baseline * threshold, 4) if baseline else None
        rec = {
            "ts": datetime.now(_JST).isoformat(timespec="seconds"),
            "verdict": verdict, "mode": mode, "commit": commit or "",
            "current": current, "baseline": baseline, "floor": floor,
            "current_n": current_n, "current_pass": current_pass,
        }
        VERDICT_LOG.parent.mkdir(parents=True, exist_ok=True)
        with VERDICT_LOG.open("a", encoding="utf-8") as f:
            f.write(json.dumps(rec, ensure_ascii=False) + "\n")
    except Exception as e:
        print(f"[deploy_eval_gate] verdict log 失敗 (非致命): {e}", file=sys.stderr)


def read_summaries(summary_file: Path) -> list:
    """eval_summary jsonl を行ごとに parse して list で返す (壊れた行は skip)。"""
    if not summary_file.exists():
        return []
    out = []
    for line in summary_file.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            out.append(json.loads(line))
        except Exception:
            continue
    return out


def last_combined_rate(summaries: list):
    """最新 summary の combined_pass_rate を返す。無ければ None。"""
    if not summaries:
        return None
    rate = summaries[-1].get("combined_pass_rate")
    try:
        return float(rate)
    except (TypeError, ValueError):
        return None


def last_summary_counts(summaries: list):
    """最新 summary の (combined_pass_rate, n_examples, n_combined_pass)。欠損は None。"""
    if not summaries:
        return None, None, None
    s = summaries[-1]

    def _f(v):
        try:
            return float(v)
        except (TypeError, ValueError):
            return None

    def _i(v):
        try:
            return int(v)
        except (TypeError, ValueError):
            return None

    return _f(s.get("combined_pass_rate")), _i(s.get("n_examples")), _i(s.get("n_combined_pass"))


# ★2026-06-08 評価 LLMOps G4: eval が小サンプルだと judge 1 回の flake で pass_rate が揺れ、
# 点推定の threshold 比較は正常 deploy を誤 rollback しうる (G1 を block 化した時の新事故源)。
# Wilson 95% 上側信頼限界で「楽観的に見ても floor を割る = 統計的に有意な劣化」のみ regression に。
MIN_N_FOR_SIG = 5  # これ未満は Wilson が不安定 → 点推定 fallback


def _wilson_upper(k: int, n: int, z: float = 1.96) -> float:
    """proportion k/n の Wilson 95% 上側信頼限界 (0-1)。"""
    if n <= 0:
        return 1.0
    k = max(0, min(k, n))
    phat = k / n
    z2 = z * z
    denom = 1.0 + z2 / n
    center = (phat + z2 / (2 * n)) / denom
    margin = (z / denom) * ((phat * (1 - phat) / n + z2 / (4 * n * n)) ** 0.5)
    return min(1.0, center + margin)


def decide(current, baseline, threshold: float, current_pass=None, current_n=None):
    """regression 判定の純関数。返り値 (verdict, detail)。

    verdict は 'regression' / 'ok' / 'inconclusive'。
    baseline が無い・0 以下、current が無い場合は inconclusive (= fail-open 側)。
    ★G4: current_n/current_pass があれば Wilson 上側信頼限界で有意性判定 (flake 誤発火回避)。
    無ければ従来の点推定 fallback。
    """
    if current is None:
        return "inconclusive", "current combined_pass_rate を取得できず"
    if baseline is None or baseline <= 0:
        return "inconclusive", f"baseline 不在/不正 (baseline={baseline}) → 比較不能"
    floor = baseline * threshold

    # ★G4: 有意性検定 (sample size が取れて十分大きい時)
    if current_n is not None and current_n >= MIN_N_FOR_SIG and current_pass is not None:
        upper = _wilson_upper(current_pass, current_n)
        if upper < floor:
            return "regression", (
                f"Wilson 上側 {upper:.3f} < floor {floor:.3f} "
                f"(rate {current:.3f}, n={current_n}, 統計的に有意な劣化)"
            )
        return "ok", (
            f"Wilson 上側 {upper:.3f} >= floor {floor:.3f} "
            f"(rate {current:.3f}, n={current_n} → 低下はノイズと区別不能)"
        )

    # fallback: 点推定 (n 不明 or 小サンプル)
    if current < floor:
        return "regression", (
            f"combined_pass_rate {current:.3f} < floor {floor:.3f} "
            f"(baseline {baseline:.3f}×{threshold}、n 不明=有意性検定なし)"
        )
    return "ok", f"combined_pass_rate {current:.3f} >= {floor:.3f} (baseline {baseline:.3f})"


def _trigger_and_wait(base_url, token, summary_file, baseline_count, timeout_s, poll_s=10):
    """POST /api/eval/run → eval_summary に新 1 行が増えるまで poll。新 summary list を返す。

    HTTP/IO の失敗は例外で上に投げ、main 側で fail-open する。
    """
    import httpx

    url = f"{base_url.rstrip('/')}/api/eval/run"
    with httpx.Client(timeout=30.0) as client:
        resp = client.post(url, params={"token": token}, json={"version": "v1"})
        resp.raise_for_status()

    deadline = time.time() + timeout_s
    while time.time() < deadline:
        time.sleep(poll_s)
        summaries = read_summaries(summary_file)
        if len(summaries) > baseline_count:
            return summaries
    raise TimeoutError(f"eval が {timeout_s}s 以内に完了せず (新 summary 行なし)")


def main() -> int:
    ap = argparse.ArgumentParser(description="post-deploy eval gate (fail-open)")
    ap.add_argument("--base-url", default="http://localhost:8000")
    ap.add_argument("--token", default="")
    ap.add_argument("--threshold", type=float, default=0.9,
                    help="baseline×threshold を割ったら regression (default 0.9)")
    ap.add_argument("--timeout", type=int, default=300, help="eval 完了待ちの上限秒 (default 300)")
    ap.add_argument("--summary-file", default=str(DEFAULT_SUMMARY))
    ap.add_argument("--mode", default="warn", help="warn|block (verdict ログ用ラベル)")
    ap.add_argument("--commit", default="", help="deploy 対象 commit hash (verdict ログ用)")
    args = ap.parse_args()

    summary_file = Path(args.summary_file)

    # fail-open: ここから先の如何なるエラーも exit 0 (= deploy を止めない)。regression のみ exit 1。
    try:
        before = read_summaries(summary_file)
        # ★2026-07-02 監査B + cross-check DA (推奨#4): eval の bot が CLONE_PUBLIC_PROD_MODEL に
        # 追随するようになったため、baseline は「同じ bot_model の直近 summary」に限定する
        # (異モデルの pass_rate と比較すると切替時に偽 regression / 手動 --bot-model 実行での
        # baseline 汚染が起きる)。モデル切替検知時は情報表示のみ (baseline は新モデルで再蓄積)。
        import os as _os
        _expected_model = _os.getenv("CLONE_PUBLIC_PROD_MODEL", "smart")
        if before and before[-1].get("bot_model", "smart") != _expected_model:
            print(f"[deploy_eval_gate] bot_model 切替検知: {before[-1].get('bot_model')} → "
                  f"{_expected_model} (baseline は同モデル分に限定して再蓄積)")
        before_same = [s for s in before if s.get("bot_model", "smart") == _expected_model]
        baseline = last_combined_rate(before_same)
        if not args.token:
            print("[deploy_eval_gate] token 未指定 → inconclusive (skip)", file=sys.stderr)
            return 0

        after = _trigger_and_wait(
            args.base_url, args.token, summary_file, len(before), args.timeout
        )
        current, current_n, current_pass = last_summary_counts(after)
        verdict, detail = decide(current, baseline, args.threshold,
                                 current_pass=current_pass, current_n=current_n)
        print(f"[deploy_eval_gate] {verdict}: {detail}")
        _log_verdict(verdict, current, baseline, args.threshold,
                     current_n, current_pass, args.mode, args.commit)
        if verdict == "regression":
            return 1
        return 0
    except Exception as e:
        # eval 障害は deploy を止めない (fail-open)
        print(f"[deploy_eval_gate] inconclusive (fail-open): {type(e).__name__}: {e}",
              file=sys.stderr)
        return 0


if __name__ == "__main__":
    sys.exit(main())
