"""smoke test: scripts/run_openai_fine_tune.py (★2026-05-23 海山指示 A-4)

OpenAI API 直叩きの dry-run 系 + validate ロジックのみ test。
実 API call は本番でのみ走る、ここは構造 sanity + validate logic。
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(REPO / "scripts"))


@pytest.mark.smoke
def test_module_imports():
    """script が import 可能。"""
    import importlib
    import run_openai_fine_tune as oft
    importlib.reload(oft)
    assert hasattr(oft, "validate")
    assert hasattr(oft, "upload")
    assert hasattr(oft, "create_job")
    assert hasattr(oft, "check")
    assert hasattr(oft, "list_models")


@pytest.mark.smoke
def test_validate_valid_dataset(tmp_path):
    """正常 dataset の validate でカウント取得。"""
    dataset = tmp_path / "valid.jsonl"
    records = [
        {"messages": [
            {"role": "system", "content": "system msg"},
            {"role": "user", "content": "user query"},
            {"role": "assistant", "content": "海山的応答"},
        ]},
        {"messages": [
            {"role": "user", "content": "別の query"},
            {"role": "assistant", "content": "別の応答"},
        ]},
    ]
    dataset.write_text(
        "\n".join(json.dumps(r, ensure_ascii=False) for r in records),
        encoding="utf-8",
    )

    from run_openai_fine_tune import validate
    result = validate(dataset)
    assert result["n_records_total"] == 2
    assert result["n_records_valid"] == 2
    assert result["n_records_invalid"] == 0
    assert "cost_estimate_3epoch_usd" in result
    assert result["cost_estimate_3epoch_usd"]["gpt-4o-mini"] >= 0
    assert "gpt-5.4-mini" in result["cost_estimate_3epoch_usd"]
    # role 集計
    assert result["role_distribution"]["user"] >= 2
    assert result["role_distribution"]["assistant"] >= 2


@pytest.mark.smoke
def test_validate_invalid_records(tmp_path):
    """role 不足 / json parse 失敗が invalid カウント。"""
    dataset = tmp_path / "mixed.jsonl"
    lines = [
        # 正常
        json.dumps({"messages": [
            {"role": "user", "content": "q"},
            {"role": "assistant", "content": "a"},
        ]}),
        # assistant 無し
        json.dumps({"messages": [
            {"role": "user", "content": "q only"},
        ]}),
        # JSON parse 失敗
        "this is not json",
        # messages 無し
        json.dumps({"foo": "bar"}),
    ]
    dataset.write_text("\n".join(lines), encoding="utf-8")

    from run_openai_fine_tune import validate
    result = validate(dataset)
    assert result["n_records_total"] == 4
    assert result["n_records_valid"] == 1
    assert result["n_records_invalid"] == 3
    # invalid sample 含まれる
    assert len(result["invalid_examples"]) >= 1


@pytest.mark.smoke
def test_validate_handles_missing_file():
    """存在しない file は SystemExit。"""
    from run_openai_fine_tune import validate
    with pytest.raises(SystemExit):
        validate(Path("/tmp/nonexistent.jsonl"))


@pytest.mark.smoke
def test_headers_requires_api_key(monkeypatch):
    """OPENAI_API_KEY 未設定で _headers() が SystemExit。"""
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    import importlib
    import run_openai_fine_tune
    importlib.reload(run_openai_fine_tune)
    with pytest.raises(SystemExit):
        run_openai_fine_tune._headers()


@pytest.mark.smoke
def test_log_event_writes_jsonl(tmp_path, monkeypatch):
    """jobs.jsonl に 1 行 JSON 追記される。"""
    monkeypatch.setenv("BRAIN_APP_ROOT", str(tmp_path))
    import importlib
    import run_openai_fine_tune
    importlib.reload(run_openai_fine_tune)

    run_openai_fine_tune._log_event("test_event", job_id="ftjob_xxx", status="running")
    log_file = tmp_path / "data" / "brain" / "fine_tune" / "jobs.jsonl"
    assert log_file.exists()
    line = log_file.read_text(encoding="utf-8").strip()
    rec = json.loads(line)
    assert rec["event"] == "test_event"
    assert rec["job_id"] == "ftjob_xxx"
    assert rec["status"] == "running"


@pytest.mark.smoke
def test_cli_help():
    """argparse の --help が動く (= syntax 完整)。"""
    import subprocess
    repo = Path(__file__).resolve().parent.parent.parent
    r = subprocess.run(
        ["python3", str(repo / "scripts" / "run_openai_fine_tune.py"), "--help"],
        capture_output=True, text=True, timeout=10,
    )
    assert r.returncode == 0
    # 主要 option が help に含まれる
    assert "--validate" in r.stdout
    assert "--upload" in r.stdout
    assert "--create-job" in r.stdout
    assert "--check" in r.stdout
    assert "--list-models" in r.stdout
