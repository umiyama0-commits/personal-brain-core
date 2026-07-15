"""smoke test: clone_hallucination_check (post-hoc fact verifier)."""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(REPO_ROOT / "scripts"))


@pytest.mark.smoke
def test_module_imports():
    """script が import できる + 必要関数が存在。"""
    import clone_hallucination_check as mod
    assert hasattr(mod, "extract_claims")
    assert hasattr(mod, "verify_claim")
    assert hasattr(mod, "gather_evidence")
    assert hasattr(mod, "run_check")
    assert hasattr(mod, "sample_turns")
    assert hasattr(mod, "CLAIM_EXTRACT_PROMPT")
    assert hasattr(mod, "VERIFY_PROMPT")


@pytest.mark.smoke
def test_verdict_categories_in_prompt():
    """VERIFY_PROMPT に 3 値 (supported / unsupported / contradicted) が含まれる。"""
    import clone_hallucination_check as mod
    for v in ("supported", "unsupported", "contradicted"):
        assert v in mod.VERIFY_PROMPT


@pytest.mark.smoke
def test_verifier_extractor_different_models(monkeypatch):
    """採点側 (VERIFIER_MODEL) は **本番 bot と別系列** (self-evaluation loop 回避)。

    ★2026-07-05 監査 fix: 旧 assert ("smart" in VERIFIER_MODEL) は smart / smart-gpt を
    区別できず、本番 bot=smart-gpt 化で同一系列 self-eval に転落しても通っていた →
    pick_cross_family_judge 追随の両分岐を検証。"""
    import importlib
    import clone_hallucination_check as mod

    monkeypatch.delenv("HALLUCINATION_VERIFIER_MODEL", raising=False)
    # bot=Claude 系 (smart) → verifier は GPT 系
    monkeypatch.setenv("CLONE_PUBLIC_PROD_MODEL", "smart")
    mod = importlib.reload(mod)
    assert mod.VERIFIER_MODEL == "smart-gpt"
    # bot=GPT 系 (smart-gpt = 本番の現状) → verifier は Claude 系
    monkeypatch.setenv("CLONE_PUBLIC_PROD_MODEL", "smart-gpt")
    mod = importlib.reload(mod)
    assert mod.VERIFIER_MODEL == "smart"
    # env での明示 override は最優先
    monkeypatch.setenv("HALLUCINATION_VERIFIER_MODEL", "smart-gpt-pro")
    mod = importlib.reload(mod)
    assert mod.VERIFIER_MODEL == "smart-gpt-pro"
    # extractor は fast-gpt がデフォルト (cost 制御)
    assert mod.EXTRACTOR_MODEL == "fast-gpt" or "fast" in mod.EXTRACTOR_MODEL
    # 後続テストへ汚染を残さない (env teardown 前に既定状態で再 load)
    monkeypatch.delenv("HALLUCINATION_VERIFIER_MODEL", raising=False)
    monkeypatch.delenv("CLONE_PUBLIC_PROD_MODEL", raising=False)
    importlib.reload(mod)


@pytest.mark.smoke
def test_is_substantive():
    """短文 / 単純な確認応答は対象外、長文は対象。"""
    import clone_hallucination_check as mod
    assert mod._is_substantive("はい") is False
    assert mod._is_substantive("OK") is False
    assert mod._is_substantive("ありがとう。") is False
    # 80 字以上の中身ある応答は True
    long_resp = "関東Aエリアの売上は 1,500,000 円、客数 100 でした。" * 3
    assert mod._is_substantive(long_resp) is True


@pytest.mark.smoke
def test_sample_turns_pairs_user_assistant():
    """user → assistant の隣接 pair だけ抽出される。"""
    import clone_hallucination_check as mod
    records = [
        {"user_id": "u1", "role": "user", "text": "売上は?", "timestamp": "2026-05-21T10:00:00+09:00"},
        {"user_id": "u1", "role": "assistant", "text": "全社売上は今日 16,000,000 円、客数 1,000 人、客単価 16,000 円でした。関東A エリアは 1,500,000 円で 100 人、九州A エリアは 1,400,000 円で 100 人。今日は週末でやや高め。", "timestamp": "2026-05-21T10:00:30+09:00"},
        {"user_id": "u1", "role": "user", "text": "ありがとう", "timestamp": "2026-05-21T10:01:00+09:00"},
        {"user_id": "u2", "role": "user", "text": "店舗?", "timestamp": "2026-05-21T11:00:00+09:00"},
        {"user_id": "u2", "role": "assistant", "text": "OK", "timestamp": "2026-05-21T11:00:30+09:00"},  # 短すぎ → 除外
    ]
    pairs = mod.sample_turns(records, max_turns=10)
    assert len(pairs) == 1  # u1 の 1 pair だけ (u2 の応答は短すぎ)
    user, assistant = pairs[0]
    assert user["role"] == "user"
    assert assistant["role"] == "assistant"


@pytest.mark.smoke
def test_sample_turns_cap_max():
    """max_turns で後ろから (最新優先) 取れる。"""
    import clone_hallucination_check as mod
    records = []
    for i in range(20):
        records.append({"user_id": "u", "role": "user", "text": f"q{i}",
                       "timestamp": f"2026-05-21T{i:02d}:00:00+09:00"})
        records.append({"user_id": "u", "role": "assistant", "text": "x" * 100,
                       "timestamp": f"2026-05-21T{i:02d}:00:30+09:00"})

    pairs = mod.sample_turns(records, max_turns=5)
    assert len(pairs) == 5
    # 最新 5 件 (15-19)
    last_q = pairs[-1][0]["text"]
    assert last_q == "q19"


@pytest.mark.smoke
async def test_extract_claims_short_response_empty(monkeypatch):
    """短い response からは claim 0 件 (LLM 呼ばずに skip)。"""
    import clone_hallucination_check as mod
    claims = await mod.extract_claims("OK")
    assert claims == []


@pytest.mark.smoke
def test_log_path_structure():
    """LOG_PATH / HALL_DIR が IMPROVE_DIR 配下。"""
    import clone_hallucination_check as mod
    assert mod.HALL_DIR.parent == mod.IMPROVE_DIR
    assert mod.HALL_DIR.name == "hallucination"
    assert mod.LOG_PATH.parent == mod.IMPROVE_DIR
    assert mod.LOG_PATH.name == "hallucination.log.jsonl"


@pytest.mark.smoke
def test_claim_extract_prompt_excludes_subjective():
    """CLAIM_EXTRACT_PROMPT が「主観」「質問」「一般常識」を除外と明示。"""
    import clone_hallucination_check as mod
    p = mod.CLAIM_EXTRACT_PROMPT
    assert "主観" in p
    assert "質問" in p or "?" in p
    assert "一般常識" in p


@pytest.mark.smoke
def test_verify_prompt_no_general_knowledge():
    """VERIFY_PROMPT が「一般常識を持って判定しない」と明示。"""
    import clone_hallucination_check as mod
    p = mod.VERIFY_PROMPT
    assert "一般常識" in p
    assert "evidence" in p


@pytest.mark.smoke
def test_cron_install_has_hallucination():
    """cron_install.sh に hallucination 行が登録されている。"""
    p = REPO_ROOT / "scripts" / "cron_install.sh"
    text = p.read_text(encoding="utf-8")
    assert "clone_cron.sh hallucination" in text
    # 03:45 のスケジュール
    assert "45 3 * * *" in text


@pytest.mark.smoke
def test_clone_cron_has_hallucination_mode():
    """clone_cron.sh に hallucination ケースが追加されている。"""
    p = REPO_ROOT / "scripts" / "clone_cron.sh"
    text = p.read_text(encoding="utf-8")
    assert "hallucination)" in text
    assert "clone_hallucination_check.py" in text
