"""
run_openai_fine_tune.py — うみやまAI 用 OpenAI fine-tuning 実行 (★2026-05-23 海山指示 A-4)

httpx 直叩きで OpenAI API を呼ぶ (openai SDK 依存無し、既存 brain_wiki と方針一致)。

mode 5 種:
  --validate <dataset.jsonl>     : dataset の sanity check (= record / 形式 / token 推定)
  --upload <dataset.jsonl>       : Files API で upload、file_id 返す
  --create-job <file_id>         : Fine-tuning Jobs API で job 作成、job_id 返す
                                   (--model / --suffix / --epochs を指定可)
  --check <job_id>               : job 状態取得 (= status / trained_tokens / fine_tuned_model)
  --list-models                  : fine-tuned model 一覧

env:
  OPENAI_API_KEY    必須 (= 海山 OpenAI 個人 / org admin key)
  OPENAI_API_BASE   optional (default https://api.openai.com/v1)

log: data/brain/fine_tune/jobs.jsonl に 各操作を 1 行 JSON 蓄積

使い方 (海山が Mac Studio で実行):
  # 1. 事前確認
  python3 scripts/build_fine_tune_dataset.py
  python3 scripts/run_openai_fine_tune.py --validate data/brain/fine_tune/dataset_v1.jsonl

  # 2. upload
  python3 scripts/run_openai_fine_tune.py --upload data/brain/fine_tune/dataset_v1.jsonl
  # → file-abc123... を出力

  # 3. job 作成
  python3 scripts/run_openai_fine_tune.py --create-job file-abc123 \
      --model gpt-4o-mini-2024-07-18 --suffix umiyama-v1 --epochs 3
  # → ftjob-xyz789... を出力

  # 4. 進捗確認
  python3 scripts/run_openai_fine_tune.py --check ftjob-xyz789
  # → status: running → succeeded、fine_tuned_model: ft:gpt-4o-mini:...:umiyama-v1:...

  # 5. 完成 model 一覧
  python3 scripts/run_openai_fine_tune.py --list-models
"""
from __future__ import annotations

import argparse
import json
import logging
import os
import sys
from datetime import datetime, timezone, timedelta
from pathlib import Path

import httpx

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger("openai_fine_tune")

JST = timezone(timedelta(hours=9))

OPENAI_API_BASE = os.getenv("OPENAI_API_BASE", "https://api.openai.com/v1")


def _jobs_log_path() -> Path:
    """jobs.jsonl の保存先 (= env 再評価、test での monkeypatch 対応)。"""
    return Path(os.getenv("BRAIN_APP_ROOT", "/app")) / "data" / "brain" / "fine_tune" / "jobs.jsonl"


def _headers() -> dict:
    api_key = os.getenv("OPENAI_API_KEY", "")
    if not api_key:
        raise SystemExit("OPENAI_API_KEY 未設定。.env に追加して再実行")
    return {"Authorization": f"Bearer {api_key}"}


def _log_event(event: str, **fields) -> None:
    """jobs.jsonl に 1 行 JSON で記録 (= env 再評価、test 互換)。"""
    log_path = _jobs_log_path()
    record = {
        "ts": datetime.now(JST).isoformat(timespec="seconds"),
        "event": event,
        **fields,
    }
    try:
        log_path.parent.mkdir(parents=True, exist_ok=True)
        with log_path.open("a", encoding="utf-8") as f:
            f.write(json.dumps(record, ensure_ascii=False) + "\n")
    except Exception as e:
        logger.warning(f"jobs.jsonl write failed: {e}")


# ─── mode 1: validate ─────────────────────────────────────────
def validate(dataset_path: Path) -> dict:
    """dataset jsonl の sanity check + token 推定 + cost 概算。

    OpenAI fine-tune 形式:
      {"messages": [{"role": "system", "content": "..."},
                    {"role": "user", "content": "..."},
                    {"role": "assistant", "content": "..."}]}
    """
    if not dataset_path.exists():
        raise SystemExit(f"dataset not found: {dataset_path}")

    n_total = 0
    n_valid = 0
    n_invalid = 0
    invalid_examples = []
    total_chars = 0
    total_tokens_est = 0  # 1 token ~ 0.5 jp char で粗推定
    role_counts = {"system": 0, "user": 0, "assistant": 0, "other": 0}

    with dataset_path.open(encoding="utf-8") as f:
        for line_num, line in enumerate(f, 1):
            line = line.strip()
            if not line:
                continue
            n_total += 1
            try:
                rec = json.loads(line)
            except Exception as e:
                n_invalid += 1
                if len(invalid_examples) < 5:
                    invalid_examples.append({"line": line_num, "error": f"json parse: {e}"[:120]})
                continue
            msgs = rec.get("messages")
            if not isinstance(msgs, list) or not msgs:
                n_invalid += 1
                if len(invalid_examples) < 5:
                    invalid_examples.append({"line": line_num, "error": "messages missing or empty"})
                continue
            # role check
            has_user = False
            has_assistant = False
            for m in msgs:
                role = m.get("role", "other")
                content = m.get("content", "")
                if not isinstance(content, str):
                    continue
                role_counts[role if role in role_counts else "other"] = (
                    role_counts.get(role if role in role_counts else "other", 0) + 1
                )
                total_chars += len(content)
                total_tokens_est += int(len(content) * 2)  # 日本語は ~2 tokens/char
                if role == "user":
                    has_user = True
                if role == "assistant":
                    has_assistant = True
            if not has_user or not has_assistant:
                n_invalid += 1
                if len(invalid_examples) < 5:
                    invalid_examples.append({
                        "line": line_num,
                        "error": f"missing role (user={has_user}, assistant={has_assistant})",
                    })
                continue
            n_valid += 1

    # cost 推定 (= gpt-4o-mini fine-tune 価格 = $3 / 1M training tokens、3 epoch)
    cost_4o_mini_usd = (total_tokens_est * 3) / 1_000_000 * 3
    cost_5_4_mini_usd = (total_tokens_est * 3) / 1_000_000 * 5  # 推定価格
    cost_4o_usd = (total_tokens_est * 3) / 1_000_000 * 25

    result = {
        "dataset_path": str(dataset_path),
        "file_size_kb": round(dataset_path.stat().st_size / 1024, 1),
        "n_records_total": n_total,
        "n_records_valid": n_valid,
        "n_records_invalid": n_invalid,
        "invalid_examples": invalid_examples,
        "role_distribution": role_counts,
        "total_chars": total_chars,
        "total_tokens_estimated": total_tokens_est,
        "cost_estimate_3epoch_usd": {
            "gpt-4o-mini": round(cost_4o_mini_usd, 2),
            "gpt-5.4-mini": round(cost_5_4_mini_usd, 2),
            "gpt-4o": round(cost_4o_usd, 2),
        },
        "verdict": (
            "✅ OpenAI fine-tune に投入可能" if n_invalid == 0 and n_valid >= 100
            else "⚠️ 件数不足 (< 100) or invalid あり、修正推奨"
        ),
    }
    _log_event("validate", **{k: v for k, v in result.items() if k != "invalid_examples"})
    return result


# ─── mode 2: upload ───────────────────────────────────────────
def upload(dataset_path: Path) -> dict:
    """OpenAI Files API に dataset を upload、file_id を返す。"""
    if not dataset_path.exists():
        raise SystemExit(f"dataset not found: {dataset_path}")
    logger.info(f"uploading {dataset_path} to OpenAI Files API...")
    url = f"{OPENAI_API_BASE}/files"
    with dataset_path.open("rb") as f:
        files = {"file": (dataset_path.name, f, "application/jsonl")}
        data = {"purpose": "fine-tune"}
        with httpx.Client(timeout=120.0) as client:
            r = client.post(url, headers=_headers(), files=files, data=data)
            r.raise_for_status()
            resp = r.json()
    file_id = resp.get("id")
    if not file_id:
        raise SystemExit(f"upload failed, no file id: {resp}")
    logger.info(f"✅ uploaded: file_id={file_id}")
    _log_event(
        "upload",
        dataset_path=str(dataset_path),
        file_id=file_id,
        bytes=resp.get("bytes"),
    )
    return resp


# ─── mode 3: create-job ───────────────────────────────────────
def create_job(
    file_id: str,
    model: str,
    suffix: str,
    epochs: int = 3,
) -> dict:
    """Fine-tuning Jobs API で job 作成、job_id を返す。"""
    url = f"{OPENAI_API_BASE}/fine_tuning/jobs"
    body = {
        "training_file": file_id,
        "model": model,
        "suffix": suffix,
        "hyperparameters": {"n_epochs": epochs},
    }
    logger.info(f"creating fine-tune job: model={model}, suffix={suffix}, epochs={epochs}")
    with httpx.Client(timeout=60.0) as client:
        r = client.post(url, headers={**_headers(), "Content-Type": "application/json"},
                        json=body)
        r.raise_for_status()
        resp = r.json()
    job_id = resp.get("id")
    if not job_id:
        raise SystemExit(f"create-job failed: {resp}")
    logger.info(f"✅ job created: {job_id}")
    logger.info(f"   status: {resp.get('status')}")
    logger.info(f"   完了後 check: python3 scripts/run_openai_fine_tune.py --check {job_id}")
    _log_event("create_job", job_id=job_id, file_id=file_id, model=model,
               suffix=suffix, epochs=epochs)
    return resp


# ─── mode 4: check ────────────────────────────────────────────
def check(job_id: str) -> dict:
    """job 状態取得。status / trained_tokens / fine_tuned_model 等を返す。"""
    url = f"{OPENAI_API_BASE}/fine_tuning/jobs/{job_id}"
    with httpx.Client(timeout=30.0) as client:
        r = client.get(url, headers=_headers())
        r.raise_for_status()
        resp = r.json()
    status = resp.get("status")
    logger.info(f"job {job_id}: status={status}")
    logger.info(f"  trained_tokens: {resp.get('trained_tokens')}")
    logger.info(f"  fine_tuned_model: {resp.get('fine_tuned_model')}")
    if resp.get("error"):
        logger.warning(f"  error: {resp['error']}")
    _log_event("check", job_id=job_id, status=status,
               trained_tokens=resp.get("trained_tokens"),
               fine_tuned_model=resp.get("fine_tuned_model"))
    return resp


# ─── mode 5: list-models ──────────────────────────────────────
def list_models(filter_prefix: str = "ft:") -> list:
    """fine-tuned model 一覧 (= "ft:..." prefix)。"""
    url = f"{OPENAI_API_BASE}/models"
    with httpx.Client(timeout=30.0) as client:
        r = client.get(url, headers=_headers())
        r.raise_for_status()
        resp = r.json()
    models = resp.get("data", [])
    ft_models = [m for m in models if (m.get("id") or "").startswith(filter_prefix)]
    logger.info(f"fine-tuned models: {len(ft_models)}")
    for m in ft_models:
        logger.info(f"  {m.get('id')}: created {m.get('created')}, owned_by={m.get('owned_by')}")
    return ft_models


# ─── CLI ──────────────────────────────────────────────────────
def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--validate", type=Path, help="dataset jsonl 検証 + token/cost 推定")
    parser.add_argument("--upload", type=Path, help="OpenAI Files API に upload")
    parser.add_argument("--create-job", help="Fine-tuning Jobs API で job 作成 (= file_id 指定)")
    parser.add_argument("--check", help="job 状態取得 (= job_id 指定)")
    parser.add_argument("--list-models", action="store_true", help="fine-tuned model 一覧")
    # create-job 用 sub-options
    parser.add_argument("--model", default="gpt-4o-mini-2024-07-18",
                        help="base model (default: gpt-4o-mini、5.4-mini や 4o も可)")
    parser.add_argument("--suffix", default="umiyama-v1",
                        help="model 名 suffix (= ft:.../<suffix> に反映)")
    parser.add_argument("--epochs", type=int, default=3, help="epoch 数 (default 3)")
    args = parser.parse_args()

    if args.validate:
        result = validate(args.validate)
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0 if result["n_records_invalid"] == 0 else 1

    if args.upload:
        result = upload(args.upload)
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0

    if args.create_job:
        result = create_job(args.create_job, args.model, args.suffix, args.epochs)
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0

    if args.check:
        result = check(args.check)
        print(json.dumps(result, ensure_ascii=False, indent=2))
        # status が succeeded / failed なら exit code 分岐
        s = result.get("status")
        return 0 if s in ("succeeded", "running", "queued", "validating_files") else 1

    if args.list_models:
        result = list_models()
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0

    parser.print_help()
    return 1


if __name__ == "__main__":
    sys.exit(main())
