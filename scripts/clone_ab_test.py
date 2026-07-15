"""
clone_ab_test.py — online A/B test framework for うみやまAI

設計:
  「prompt 改訂 / モデル切替で応答品質が劣化したか」を、deploy 時の AB
  (prompt_diff_check) だけでなく **online ユーザに 2 バージョン振る本物 A/B** で
  検証する仕組み。

  user_id から bucket を決定論的に振り分け:
    bucket_A: user_id_hash % 2 == 0  (例: モデル smart = Claude Opus 4.8)
    bucket_B: user_id_hash % 2 == 1  (例: モデル smart-gpt = GPT-5.4)

  bucket は 1 セッション中変わらない (確定性)、ただし新 A/B 開始時は実験 ID で reset。

  bot_events.jsonl に bucket フィールド付きで記録 → 1 週間後に集計:
    - 平均 latency / response_chars / 失敗率
    - 各 bucket の clone_feedback (修正希望) 比率
    - 各 bucket の implicit feedback (会話継続率 etc.)

実行:
  python3 scripts/clone_ab_test.py --new "smart-vs-smart-gpt-2026-05" \\
                                    --bucket-a-model smart --bucket-b-model smart-gpt
       # 新実験を作成

  python3 scripts/clone_ab_test.py --status                # 現在の実験一覧
  python3 scripts/clone_ab_test.py --analyze "smart-vs-smart-gpt-2026-05"
       # 直近 7 日の bot_events から bucket A/B を比較

API:
  bucket = ab_test.assign_bucket(user_id, experiment_id)
  → "A" or "B" or "control" (実験 未稼働なら "control")

設計上の単純さ:
  - 実験定義は data/brain/clone_improve/ab_experiments/<id>.json に 1 ファイル
  - active な実験は 1 つを想定 (シンプルな状態管理)
  - 完了時に手動で finalize して "completed" にする (auto stop しない)
"""
from __future__ import annotations

import argparse
import hashlib
import json
import logging
import os
import sys
from datetime import datetime, timezone, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from clone_improve_lib import JST  # noqa: E402

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger("clone_ab_test")


def _ab_dir() -> Path:
    """test 環境で BRAIN_APP_ROOT が変わっても追従するための遅延評価。"""
    app_root = Path(os.getenv("BRAIN_APP_ROOT", "/app"))
    return app_root / "data" / "brain" / "clone_improve" / "ab_experiments"


def _active_path() -> Path:
    return _ab_dir() / "_active.json"


# ─── 実験定義 ───────────────────────────────
def _exp_path(experiment_id: str) -> Path:
    return _ab_dir() / f"{experiment_id}.json"


def create_experiment(
    experiment_id: str,
    bucket_a: dict,
    bucket_b: dict,
    description: str = "",
    set_active: bool = True,
) -> dict:
    """新 A/B 実験を作成。

    bucket_a / bucket_b は {"model": "smart", "prompt_variant": "..."} 等の dict。
    """
    _ab_dir().mkdir(parents=True, exist_ok=True)
    exp = {
        "id": experiment_id,
        "created_at": datetime.now(JST).isoformat(),
        "status": "active",
        "description": description,
        "bucket_a": bucket_a,
        "bucket_b": bucket_b,
        "completed_at": None,
    }
    _exp_path(experiment_id).write_text(
        json.dumps(exp, ensure_ascii=False, indent=2), encoding="utf-8"
    )

    if set_active:
        _active_path().write_text(
            json.dumps({"active_experiment": experiment_id}, ensure_ascii=False),
            encoding="utf-8",
        )

    logger.info(f"experiment created: {experiment_id} (active={set_active})")
    return exp


def get_active_experiment_id() -> str | None:
    p = _active_path()
    if not p.exists():
        return None
    try:
        return json.loads(p.read_text(encoding="utf-8")).get("active_experiment")
    except Exception:
        return None


def get_experiment(experiment_id: str) -> dict | None:
    p = _exp_path(experiment_id)
    if not p.exists():
        return None
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except Exception:
        return None


def finalize_experiment(experiment_id: str) -> bool:
    """active から外して completed に。"""
    exp = get_experiment(experiment_id)
    if not exp:
        return False
    exp["status"] = "completed"
    exp["completed_at"] = datetime.now(JST).isoformat()
    _exp_path(experiment_id).write_text(
        json.dumps(exp, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    if get_active_experiment_id() == experiment_id:
        _active_path().write_text(
            json.dumps({"active_experiment": None}, ensure_ascii=False),
            encoding="utf-8",
        )
    logger.info(f"experiment finalized: {experiment_id}")
    return True


# ─── bucket 振り分け ────────────────────────
def assign_bucket(user_id: str, experiment_id: str | None = None) -> str:
    """user_id を A/B/control に振り分け。

    実験が active でなければ "control" を返す。
    決定論的: 同じ user_id + experiment_id は常に同じ bucket。
    """
    exp_id = experiment_id or get_active_experiment_id()
    if not exp_id:
        return "control"
    exp = get_experiment(exp_id)
    if not exp or exp.get("status") != "active":
        return "control"

    # user_id を hash → 偶奇で A/B
    seed = f"{exp_id}:{user_id}".encode("utf-8")
    h = int(hashlib.md5(seed).hexdigest(), 16)
    return "A" if h % 2 == 0 else "B"


def get_bucket_config(user_id: str, experiment_id: str | None = None) -> dict:
    """user_id に該当する bucket の config (model 等) を返す。

    bucket="control" の場合は {"bucket": "control"} のみ。
    """
    exp_id = experiment_id or get_active_experiment_id()
    if not exp_id:
        return {"bucket": "control"}
    exp = get_experiment(exp_id)
    if not exp or exp.get("status") != "active":
        return {"bucket": "control"}

    b = assign_bucket(user_id, exp_id)
    if b == "control":
        return {"bucket": "control", "experiment_id": exp_id}
    cfg = exp[f"bucket_{b.lower()}"] or {}
    return {**cfg, "bucket": b, "experiment_id": exp_id}


# ─── 集計 ────────────────────────────────────
def analyze_experiment(experiment_id: str, days: int = 7) -> dict:
    """bot_events.jsonl から該当 experiment の A/B を比較。"""
    try:
        from bot_events import iter_events  # type: ignore
    except Exception as e:
        return {"status": "no_bot_events", "error": str(e)}

    exp = get_experiment(experiment_id)
    if not exp:
        return {"status": "experiment_not_found"}

    since_sec = days * 86400
    events = list(iter_events(since_sec=since_sec))

    # bucket フィールドが立ってる turn_finished を集める
    by_bucket: dict[str, dict] = {
        "A": {"n": 0, "n_failed": 0, "latencies_ms": [], "response_chars": []},
        "B": {"n": 0, "n_failed": 0, "latencies_ms": [], "response_chars": []},
    }
    for e in events:
        if e.get("experiment_id") != experiment_id:
            continue
        b = e.get("bucket")
        if b not in ("A", "B"):
            continue
        ev = e.get("event")
        if ev == "turn_finished":
            by_bucket[b]["n"] += 1
            try:
                by_bucket[b]["latencies_ms"].append(float(e.get("elapsed_ms", 0)))
            except Exception:
                pass
            try:
                by_bucket[b]["response_chars"].append(int(e.get("response_chars", 0)))
            except Exception:
                pass
        elif ev == "turn_failed":
            by_bucket[b]["n_failed"] += 1

    def _stats(lst: list[float]) -> dict:
        if not lst:
            return {"n": 0, "mean": 0.0, "p50": 0, "p95": 0}
        s = sorted(lst)
        mean = sum(s) / len(s)
        p50 = s[len(s) // 2]
        p95 = s[int(len(s) * 0.95)] if len(s) > 1 else s[0]
        return {"n": len(s), "mean": round(mean, 1), "p50": round(p50, 1),
                "p95": round(p95, 1)}

    report = {
        "experiment_id": experiment_id,
        "window_days": days,
        "bucket_a": {
            "config": exp["bucket_a"],
            "n_finished": by_bucket["A"]["n"],
            "n_failed": by_bucket["A"]["n_failed"],
            "fail_rate": round(by_bucket["A"]["n_failed"] /
                               max(1, by_bucket["A"]["n"] + by_bucket["A"]["n_failed"]), 3),
            "latency_ms": _stats(by_bucket["A"]["latencies_ms"]),
            "response_chars": _stats([float(x) for x in by_bucket["A"]["response_chars"]]),
        },
        "bucket_b": {
            "config": exp["bucket_b"],
            "n_finished": by_bucket["B"]["n"],
            "n_failed": by_bucket["B"]["n_failed"],
            "fail_rate": round(by_bucket["B"]["n_failed"] /
                               max(1, by_bucket["B"]["n"] + by_bucket["B"]["n_failed"]), 3),
            "latency_ms": _stats(by_bucket["B"]["latencies_ms"]),
            "response_chars": _stats([float(x) for x in by_bucket["B"]["response_chars"]]),
        },
    }

    # 結果保存
    _ab_dir().mkdir(parents=True, exist_ok=True)
    out_path = _ab_dir() / f"{experiment_id}_analysis_{datetime.now(JST).strftime('%Y%m%d')}.json"
    out_path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    return report


def list_experiments() -> list[dict]:
    d = _ab_dir()
    if not d.exists():
        return []
    out = []
    for f in sorted(d.glob("*.json")):
        if f.name.startswith("_"):
            continue
        if "_analysis_" in f.name:
            continue
        try:
            out.append(json.loads(f.read_text(encoding="utf-8")))
        except Exception:
            continue
    return out


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--new", help="新実験を作る (experiment_id)")
    ap.add_argument("--bucket-a-model", default="smart")
    ap.add_argument("--bucket-b-model", default="smart-gpt")
    ap.add_argument("--description", default="")
    ap.add_argument("--status", action="store_true", help="実験一覧 + active")
    ap.add_argument("--analyze", help="実験 ID を指定して集計")
    ap.add_argument("--days", type=int, default=7, help="集計対象 (--analyze 用)")
    ap.add_argument("--finalize", help="実験 ID を completed に")
    ap.add_argument("--assign", help="user_id を渡して bucket だけ確認 (debug 用)")
    args = ap.parse_args()

    if args.new:
        exp = create_experiment(
            args.new,
            bucket_a={"model": args.bucket_a_model},
            bucket_b={"model": args.bucket_b_model},
            description=args.description,
        )
        print(json.dumps(exp, ensure_ascii=False, indent=2))
        return 0

    if args.status:
        print(f"active experiment: {get_active_experiment_id()}")
        for e in list_experiments():
            print(f"  - {e['id']} [{e['status']}] {e.get('description', '')}")
        return 0

    if args.analyze:
        report = analyze_experiment(args.analyze, days=args.days)
        print(json.dumps(report, ensure_ascii=False, indent=2))
        return 0

    if args.finalize:
        ok = finalize_experiment(args.finalize)
        print("finalized" if ok else "not found")
        return 0

    if args.assign:
        cfg = get_bucket_config(args.assign)
        print(json.dumps(cfg, ensure_ascii=False, indent=2))
        return 0

    ap.print_help()
    return 0


if __name__ == "__main__":
    sys.exit(main())
