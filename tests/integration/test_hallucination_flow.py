"""
Integration test: hallucination check の end-to-end (LLM mock)。

clone_hallucination_check の extract_claims / verify_claim / run_check を
mock LLM で動かして全フローを検証。
"""
from __future__ import annotations

import importlib
import json
import sys
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(REPO_ROOT / "scripts"))


@pytest.fixture
def hallucination_mod(isolated_brain_root, monkeypatch):
    """clone_hallucination_check を reload + clone_improve_lib も追従。"""
    for mod_name in ("clone_improve_lib", "clone_hallucination_check"):
        if mod_name in sys.modules:
            importlib.reload(sys.modules[mod_name])
    import clone_hallucination_check as mod  # type: ignore
    return mod


@pytest.mark.integration
async def test_extract_claims_returns_list(hallucination_mod, monkeypatch):
    """LLM mock で claim 抽出が JSON parse → list 返却。"""
    async def fake_call_llm(prompt, model=None, max_tokens=None, temperature=None):
        return '```json\n{"claims": ["関東Aエリアの売上は 1,500,000 円", "客数 100"]}\n```'

    monkeypatch.setattr(hallucination_mod, "call_llm", fake_call_llm)
    claims = await hallucination_mod.extract_claims(
        "関東Aエリアの売上は 1,500,000 円、客数 100 人。" * 3
    )
    assert "関東Aエリアの売上は 1,500,000 円" in claims
    assert "客数 100" in claims


@pytest.mark.integration
async def test_verify_claim_supported(hallucination_mod, monkeypatch):
    """evidence と一致する claim は supported 判定。"""
    async def fake_call_llm(prompt, model=None, max_tokens=None, temperature=None):
        return '```json\n{"verdict": "supported", "reason": "wiki に同じ数字あり", "evidence_snippet": "1,500,000 円"}\n```'

    monkeypatch.setattr(hallucination_mod, "call_llm", fake_call_llm)
    evidence = [{"source": "knowledge/owndays-daily-sales.md",
                 "content": "関東A 1,500,000 円"}]
    result = await hallucination_mod.verify_claim(
        "関東Aエリアの売上は 1,500,000 円", evidence, "応答テキスト全体"
    )
    assert result["verdict"] == "supported"


@pytest.mark.integration
async def test_verify_claim_contradicted(hallucination_mod, monkeypatch):
    """evidence と矛盾する claim は contradicted 判定。"""
    async def fake_call_llm(prompt, model=None, max_tokens=None, temperature=None):
        return '```json\n{"verdict": "contradicted", "reason": "wiki は 100M、claim は 200M", "evidence_snippet": "...100M..."}\n```'

    monkeypatch.setattr(hallucination_mod, "call_llm", fake_call_llm)
    evidence = [{"source": "x.md", "content": "売上 100M"}]
    result = await hallucination_mod.verify_claim(
        "売上 200M", evidence, "応答"
    )
    assert result["verdict"] == "contradicted"


@pytest.mark.integration
async def test_run_check_full_flow_no_hist(hallucination_mod):
    """clone_history が空 → status=no_data。"""
    result = await hallucination_mod.run_check(hours=24, sample=10, dry_run=True)
    assert result["status"] == "no_data"


@pytest.mark.integration
async def test_run_check_full_flow_with_seed(
    hallucination_mod, isolated_brain_root, monkeypatch,
):
    """clone_history を seed して run_check が turn を処理する。"""
    # clone_history 1 pair (user + assistant) を seed
    hist_dir = isolated_brain_root / "clone_history"
    now = datetime.now(timezone.utc)
    records = [
        json.dumps({
            "timestamp": now.isoformat(), "user_id": "u1", "role": "user",
            "text": "関東Aエリアの売上は?"
        }, ensure_ascii=False),
        json.dumps({
            "timestamp": now.isoformat(), "user_id": "u1", "role": "assistant",
            "text": "関東Aエリアの売上は 1,500,000 円、客数 100 人、客単価 15,000 円でした。"
                    "今日は平日にしては高めの数字。先週の同曜日と比べても約 10% 高め、"
                    "イベントや天気の影響を見たほうがいい。"
        }, ensure_ascii=False),
    ]
    target_file = hist_dir / "u1.jsonl"
    target_file.write_text("\n".join(records) + "\n", encoding="utf-8")

    # claim extract / verify を mock
    call_log = []

    async def fake_call_llm(prompt, model=None, max_tokens=None, temperature=None):
        call_log.append({"model": model, "prompt_head": prompt[:100]})
        if "fact-extraction" in prompt or "factual claim" in prompt:
            return '```json\n{"claims": ["関東Aエリア売上 1,500,000 円", "客数 100"]}\n```'
        if "fact-verification" in prompt or "verdict" in prompt:
            return '```json\n{"verdict": "supported", "reason": "wiki と一致", "evidence_snippet": "関東A 1,500,000 円"}\n```'
        return "{}"

    monkeypatch.setattr(hallucination_mod, "call_llm", fake_call_llm)
    result = await hallucination_mod.run_check(
        hours=24, sample=5, dry_run=True, brain_wiki=None
    )
    assert result.get("n_turns_checked") == 1, f"unexpected result: {result}"
    assert result["n_claims_total"] == 2
    assert result["verdicts"]["supported"] == 2
    assert result["verdicts"]["contradicted"] == 0
    # claim extract (1 回) + verify (2 claim) = 3 回 LLM 呼ばれる
    assert len(call_log) == 3


@pytest.mark.integration
async def test_run_check_dry_run_doesnt_write(
    hallucination_mod, isolated_brain_root, monkeypatch,
):
    """dry_run=True なら結果 JSON / log 書き込み無し。"""
    hist_dir = isolated_brain_root / "clone_history"
    now = datetime.now(timezone.utc)
    (hist_dir / "u1.jsonl").write_text(
        json.dumps({"timestamp": now.isoformat(), "user_id": "u1", "role": "user",
                    "text": "売上"}, ensure_ascii=False) + "\n" +
        json.dumps({"timestamp": now.isoformat(), "user_id": "u1", "role": "assistant",
                    "text": "売上は 100M" * 10}, ensure_ascii=False) + "\n",
        encoding="utf-8"
    )

    async def fake_call_llm(*a, **kw):
        return '```json\n{"claims": []}\n```'

    monkeypatch.setattr(hallucination_mod, "call_llm", fake_call_llm)
    await hallucination_mod.run_check(hours=24, sample=5, dry_run=True)
    hall_json = list((isolated_brain_root / "clone_improve" / "hallucination").glob("*.json")) \
        if (isolated_brain_root / "clone_improve" / "hallucination").exists() else []
    assert hall_json == []
