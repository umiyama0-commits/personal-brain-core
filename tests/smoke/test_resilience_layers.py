"""smoke test: レジリエンス Layer 1-3 統合 (★2026-05-23 海山指示)

3 層の障害検知:
- Layer 1: 応答品質 judge の fallback 連発検知 + LINE Push
- Layer 2: LiteLLM fallbacks 多段化 (smart → smart-fallback → smart-gpt)
- Layer 3: clone_respond_public の except で bot_events に fallback_returned を記録
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(REPO / "scripts"))


# ─── Layer 3: brain_wiki.clone_respond_public の except 内 fallback 記録 ─────
@pytest.mark.smoke
def test_clone_respond_public_logs_fallback_to_bot_events():
    """clone_respond_public の except 節で bot_events.log_bot_event(fallback_returned) を呼ぶ。"""
    src = (REPO / "brain_wiki.py").read_text(encoding="utf-8")

    # except 節周辺を抽出
    idx = src.find('return "お休みをいただいてます。しばらく経ってから再度試して。"')
    assert idx > 0, "fallback 文言が見つからない"
    # その近傍 (前 700 char) に log_bot_event がある
    window = src[max(0, idx - 700):idx]
    assert "log_bot_event" in window
    assert "fallback_returned" in window
    # bot_events 経路から logging を import している
    assert "from scripts.bot_events import log_bot_event" in window


@pytest.mark.smoke
def test_clone_respond_public_logging_failure_is_swallowed():
    """logging 自体が失敗しても本流 (fallback 文言 return) は止まらない (= silent except)。"""
    src = (REPO / "brain_wiki.py").read_text(encoding="utf-8")
    idx = src.find('return "お休みをいただいてます。しばらく経ってから再度試して。"')
    window = src[max(0, idx - 700):idx]
    # log_bot_event 周辺に try/except があり、Exception を pass している
    # (= logging 失敗で fallback 返答が遅延・失敗しない)
    assert "except Exception:" in window
    assert "pass" in window


# ─── Layer 1 と 2 の cron / config 統合確認 ─────
@pytest.mark.smoke
def test_layer1_fallback_alert_runs_in_existing_cron():
    """応答品質 judge の cron が 30 分ごと → fallback 検知も 30 分以内に発火する。"""
    src = (REPO / "scripts" / "cron_install.sh").read_text(encoding="utf-8")
    # 既存 response-quality cron が */30 で登録されている
    assert "*/30 * * * *" in src
    assert "clone_cron.sh response-quality" in src
    # clone_response_quality_judge が fallback 検知ロジックを持つ
    judge_src = (REPO / "scripts" / "clone_response_quality_judge.py").read_text(encoding="utf-8")
    assert "🚨 うみやまAI fallback 連発検知" in judge_src
    assert "FALLBACK_ALERT_RATIO" in judge_src
    # 復旧手順が Push 本文に含まれる (= 海山が即対応できる)
    assert "docker ps | grep line-bot" in judge_src
    assert "docker logs line-bot" in judge_src


@pytest.mark.smoke
def test_layer2_litellm_fallback_chain_includes_smart_gpt():
    """smart の fallback chain に GPT-5.4 (smart-gpt) も入っている。"""
    cfg = (REPO / "litellm_config.yaml").read_text(encoding="utf-8")
    # smart fallback chain
    import re
    m = re.search(r"-\s*smart:\s*\[([^\]]+)\]", cfg)
    assert m, "smart fallback line not found"
    chain = m.group(1)
    assert "smart-fallback" in chain  # GPT-4o
    assert "smart-gpt" in chain  # GPT-5.4
    # fast の fallback chain
    m2 = re.search(r"-\s*fast:\s*\[([^\]]+)\]", cfg)
    assert m2, "fast fallback line not found"


@pytest.mark.smoke
def test_three_layers_overall_constants():
    """Layer 1-3 の env override が現実的な値に。"""
    from clone_response_quality_judge import (
        FALLBACK_ALERT_RATIO, FALLBACK_ALERT_MIN_TOTAL, FALLBACK_PHRASES,
    )
    # alert ratio = 30% default (= 5/22 事案で 100% fallback だった → 余裕で検出される)
    assert FALLBACK_ALERT_RATIO == 0.3 or 0.2 <= FALLBACK_ALERT_RATIO <= 0.5
    # 3 件以上の応答が来てから判定 (= 真夜中の少件数で誤検出しない)
    assert FALLBACK_ALERT_MIN_TOTAL >= 2
    # 主要 fallback phrase をカバー
    assert len(FALLBACK_PHRASES) >= 2
