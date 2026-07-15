"""smoke test: scripts/clone_response_quality_judge.py (★2026-05-23 海山指示 打ち手 B)

LLM 呼び出しは mock 化、pure 関数 (sample_turns / _is_substantive_response) + JSON parse の
正常系・境界・threshold 判定だけ smoke 化。
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(REPO / "scripts"))


@pytest.mark.smoke
def test_substantive_response_filter():
    from clone_response_quality_judge import _is_substantive_response

    # 短すぎる = 除外 (相槌のみ)
    assert _is_substantive_response("") is False
    assert _is_substantive_response("はい。") is False
    assert _is_substantive_response("OK 了解です。") is False
    # fallback 文言 = 除外
    assert _is_substantive_response("お休みをいただいてます。しばらく経ってから") is False
    assert _is_substantive_response("申し訳ありません。少し時間を置いて") is False
    # 通常応答 = 採点対象
    assert _is_substantive_response("サンプル駅前店、今日の数字は客数 12 / 売上 38 万 / 単価 31700 円。") is True
    assert _is_substantive_response("そうね、元気出ない時あるよね。無理に上げようとしないことが大事。") is True


@pytest.mark.smoke
def test_sample_turns_pairs_user_assistant():
    from clone_response_quality_judge import sample_turns

    records = [
        {"user_id": "u1", "role": "user", "text": "短い質問", "timestamp": "2026-05-23T10:00:00"},
        {"user_id": "u1", "role": "assistant", "text": "これは substantive な応答 (30 字を超えてるはず)。" * 2, "timestamp": "2026-05-23T10:00:05"},
        {"user_id": "u1", "role": "user", "text": "次の質問", "timestamp": "2026-05-23T10:01:00"},
        {"user_id": "u1", "role": "assistant", "text": "もう一度 substantive な応答 (これも 30 字超え)。" * 2, "timestamp": "2026-05-23T10:01:05"},
    ]
    pairs = sample_turns(records, max_turns=10)
    assert len(pairs) == 2
    # 各 pair が (user, assistant) になってる
    for u, a in pairs:
        assert u["role"] == "user"
        assert a["role"] == "assistant"


@pytest.mark.smoke
def test_sample_turns_excludes_fallback():
    from clone_response_quality_judge import sample_turns

    records = [
        {"user_id": "u1", "role": "user", "text": "テスト", "timestamp": "2026-05-23T10:00:00"},
        {"user_id": "u1", "role": "assistant", "text": "お休みをいただいてます。しばらく経ってから再度試して。", "timestamp": "2026-05-23T10:00:05"},
    ]
    pairs = sample_turns(records, max_turns=10)
    # fallback 応答は pair に入らない
    assert len(pairs) == 0


@pytest.mark.smoke
def test_sample_turns_respects_max_turns():
    from clone_response_quality_judge import sample_turns

    records = []
    for i in range(20):
        records.append({"user_id": "u1", "role": "user", "text": f"q{i}", "timestamp": f"2026-05-23T10:{i:02d}:00"})
        records.append({"user_id": "u1", "role": "assistant", "text": "substantive 応答 30 字超え。" * 3, "timestamp": f"2026-05-23T10:{i:02d}:05"})
    pairs = sample_turns(records, max_turns=5)
    # 最新 5 件のみ
    assert len(pairs) == 5
    # 最新優先: ts が大きい順
    assert pairs[-1][0]["text"] == "q19"


@pytest.mark.smoke
def test_judge_prompt_contains_three_axes():
    """JUDGE_PROMPT に 3 軸の指示が入ってる (= 構造 sanity)。"""
    from clone_response_quality_judge import JUDGE_PROMPT
    assert "ai_smell" in JUDGE_PROMPT
    assert "mirroring_fit" in JUDGE_PROMPT
    assert "length_appropriate" in JUDGE_PROMPT
    # 海山の核軸が prompt に入ってる
    assert "AI 臭" in JUDGE_PROMPT or "AI臭" in JUDGE_PROMPT
    assert "ミラーリング" in JUDGE_PROMPT
    # 1-5 採点
    assert "1-5" in JUDGE_PROMPT or "1〜5" in JUDGE_PROMPT


@pytest.mark.smoke
def test_judge_model_is_different_series(monkeypatch):
    """judge model は **本番 bot と別系列** (= self-eval loop 回避の制約)。

    ★2026-07-05 監査 fix: 旧 assert は「judge=GPT 系固定」を pin していたが、本番 bot が
    CLONE_PUBLIC_PROD_MODEL=smart-gpt (GPT 系) に切替済みだと GPT judge = 同一系列 self-eval。
    正しい不変条件は「bot の系列と judge の系列が異なる」— 両分岐を検証する。"""
    import importlib
    import clone_response_quality_judge as mod

    monkeypatch.delenv("RESPONSE_QUALITY_JUDGE_MODEL", raising=False)
    # bot=Claude 系 (smart) → judge は GPT 系
    monkeypatch.setenv("CLONE_PUBLIC_PROD_MODEL", "smart")
    mod = importlib.reload(mod)
    assert mod.JUDGE_MODEL == "smart-gpt"
    # bot=GPT 系 (smart-gpt = 本番の現状) → judge は Claude 系
    monkeypatch.setenv("CLONE_PUBLIC_PROD_MODEL", "smart-gpt")
    mod = importlib.reload(mod)
    assert mod.JUDGE_MODEL == "smart"
    # env での明示 override は最優先
    monkeypatch.setenv("RESPONSE_QUALITY_JUDGE_MODEL", "fast-gpt")
    mod = importlib.reload(mod)
    assert mod.JUDGE_MODEL == "fast-gpt"
    # 後続テストへ汚染を残さない (env teardown 前に既定状態で再 load)
    monkeypatch.delenv("RESPONSE_QUALITY_JUDGE_MODEL", raising=False)
    monkeypatch.delenv("CLONE_PUBLIC_PROD_MODEL", raising=False)
    importlib.reload(mod)


@pytest.mark.smoke
def test_thresholds_are_int():
    """env override される threshold が int 型で読まれる。"""
    from clone_response_quality_judge import DEGRADED_THRESHOLD, PUSH_THRESHOLD, MAX_TURNS_PER_RUN
    assert isinstance(DEGRADED_THRESHOLD, int)
    assert isinstance(PUSH_THRESHOLD, int)
    assert isinstance(MAX_TURNS_PER_RUN, int)
    # 健全な値域
    assert 1 <= DEGRADED_THRESHOLD <= 5
    assert PUSH_THRESHOLD >= 1
    assert MAX_TURNS_PER_RUN >= 1


@pytest.mark.smoke
def test_cron_install_includes_response_quality():
    """cron_install.sh の REQUIRED_CRONS に response-quality entry が入ってる。"""
    src = (REPO / "scripts" / "cron_install.sh").read_text(encoding="utf-8")
    assert "response-quality" in src
    # 30 分ごと
    assert "*/30 * * * *" in src
    # PATTERNS にも入ってる
    assert "clone_cron.sh response-quality" in src


@pytest.mark.smoke
def test_clone_cron_sh_handles_response_quality_mode():
    """clone_cron.sh が response-quality モードを処理する case を持つ。"""
    src = (REPO / "scripts" / "clone_cron.sh").read_text(encoding="utf-8")
    assert "response-quality)" in src
    assert "clone_response_quality_judge.py" in src
    # help にも記載されてる
    assert "response-quality" in src


@pytest.mark.smoke
def test_script_imports_clone_improve_lib():
    """clone_improve_lib の共通基盤を使う (= 一貫性、call_llm/line_push が同じ実装)。"""
    src = (REPO / "scripts" / "clone_response_quality_judge.py").read_text(encoding="utf-8")
    assert "from clone_improve_lib import" in src
    assert "call_llm" in src
    assert "line_push" in src
    assert "append_jsonl" in src
    assert "load_conversations" in src


# ─── レジリエンス Layer 1: fallback 文言検知 (★2026-05-23 海山指示) ─────────────
@pytest.mark.smoke
def test_is_fallback_response_matches_known_phrases():
    from clone_response_quality_judge import _is_fallback_response
    assert _is_fallback_response("お休みをいただいてます。しばらく経ってから") is True
    assert _is_fallback_response("申し訳ありません。少し時間を置いて") is True
    assert _is_fallback_response("[error] internal") is True
    # 通常応答は False
    assert _is_fallback_response("サンプル駅前店、客数 12 / 売上 38 万") is False
    assert _is_fallback_response("そうね、元気出ない時あるよね") is False
    assert _is_fallback_response("") is False
    assert _is_fallback_response(None) is False


@pytest.mark.smoke
def test_count_bot_responses_counts_assistant_only():
    from clone_response_quality_judge import count_bot_responses
    records = [
        {"role": "user", "text": "質問 1"},
        {"role": "assistant", "text": "お休みをいただいてます。しばらく経ってから"},
        {"role": "user", "text": "質問 2"},
        {"role": "assistant", "text": "ふつうの応答内容です"},
        {"role": "assistant", "text": "申し訳ありません。少し時間を置いて"},
        # role 不明 / user role は無視
        {"role": "system", "text": "ignored"},
    ]
    total, fallback = count_bot_responses(records)
    assert total == 3
    assert fallback == 2


@pytest.mark.smoke
def test_count_bot_responses_empty():
    from clone_response_quality_judge import count_bot_responses
    assert count_bot_responses([]) == (0, 0)
    # user のみ
    assert count_bot_responses([{"role": "user", "text": "q"}]) == (0, 0)


@pytest.mark.smoke
def test_fallback_alert_constants_are_sane():
    """環境変数 default が現実的な値域に収まっている。"""
    from clone_response_quality_judge import (
        FALLBACK_ALERT_RATIO, FALLBACK_ALERT_MIN_TOTAL, FALLBACK_PHRASES,
    )
    assert 0.1 <= FALLBACK_ALERT_RATIO <= 1.0  # 10-100%
    assert FALLBACK_ALERT_MIN_TOTAL >= 1
    assert len(FALLBACK_PHRASES) >= 2  # 主要 fallback phrase をカバー
    # 「お休み」「申し訳」が含まれる
    assert any("お休み" in p for p in FALLBACK_PHRASES)
    assert any("申し訳" in p for p in FALLBACK_PHRASES)


# ─── レジリエンス Layer 2: LiteLLM fallbacks 多段化 ─────────────
@pytest.mark.smoke
def test_litellm_fallbacks_multi_layer():
    """litellm_config.yaml の fallbacks が smart 2 段以上、num_retries ≥ 3。"""
    cfg = (REPO / "litellm_config.yaml").read_text(encoding="utf-8")
    # smart の fallback chain
    assert "smart-fallback" in cfg
    assert "smart-gpt" in cfg
    # fast の fallback chain
    assert "fast-gpt" in cfg
    # num_retries は 5/22 事案後に強化
    assert "num_retries: 3" in cfg
    # smart の fallback 行に少なくとも 2 つ並んでる
    import re
    m = re.search(r"smart:\s*\[([^\]]+)\]", cfg)
    assert m, "smart fallback chain not found"
    n_targets = len([x for x in m.group(1).split(",") if x.strip()])
    assert n_targets >= 2, f"smart fallback chain too short ({n_targets})"
