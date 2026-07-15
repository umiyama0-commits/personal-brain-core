#!/usr/bin/env python3
"""retrieval golden eval (BM25-only) を実行し summary を追記 + loud_fail 配線。

★2026-07-10 世界基準評価 S4b: 「作った golden eval を回す」の週次運用ラッパー。
BM25-only = chromadb 非依存 = bot 停止不要 (§1.5 回避)。full dense+rerank pipeline は
bot 停止窓が要るため手動運用 (docs/runbook)。clone_cron.sh golden-eval が呼ぶ。
"""
from __future__ import annotations

import datetime
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

ROOT = Path(__file__).resolve().parent.parent
SUMMARY = ROOT / "data" / "brain" / "alignment" / "retrieval_eval_summary.jsonl"


def main() -> int:
    ok = False
    result: dict = {}
    try:
        import retrieval_eval as re  # noqa: E402
        result = re.run_bm25_only(None)
        ok = True
    except SystemExit:
        # golden 検証で gold doc 消失 → 非0 exit する設計
        ok = False
    except Exception as e:
        print(f"golden eval 実行失敗: {e}")
        ok = False

    if ok and result:
        try:
            result["ts"] = datetime.datetime.now().isoformat()
            result["variant"] = "bm25_only"
            SUMMARY.parent.mkdir(parents=True, exist_ok=True)
            with SUMMARY.open("a", encoding="utf-8") as f:
                f.write(json.dumps(result, ensure_ascii=False) + "\n")
            print(f"golden eval logged: Recall@10={result.get('recall@10')} nDCG@10={result.get('ndcg@10')}")
        except Exception as e:
            print(f"summary 追記失敗: {e}")

    try:
        from clone_improve_lib import loud_fail  # noqa: E402
        loud_fail(
            "retrieval_golden_eval", ok,
            "BM25 golden eval 失敗 (golden set の gold doc 消失 or index 構築失敗)。"
            "gold doc が rename/削除された可能性 = golden set の保守が要る",
            threshold=1, cooldown_h=168,
        )
    except Exception as e:
        print(f"loud_fail 配線失敗 (非致命): {e}")

    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
