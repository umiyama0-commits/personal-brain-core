"""smoke test: bot_metrics._format_cost_summary + clone_cost_summary.py (★2026-05-23 LEE §4.2)。

cost cap + 日次 Push の構造 sanity check。LLM 呼び出しなし、pure 関数 + cron 統合確認。
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(REPO / "scripts"))


@pytest.mark.smoke
def test_format_cost_summary_handles_empty_events():
    from bot_metrics import _format_cost_summary
    result = _format_cost_summary([])
    assert "no usage data" in result.lower() or "(no" in result


@pytest.mark.smoke
def test_format_cost_summary_basic_calculation():
    """1 turn の usage から USD 計算が走る。"""
    from bot_metrics import _format_cost_summary
    events = [
        {
            "event": "turn_finished",
            "ts": "2026-05-23T10:00:00",
            "model": "anthropic/claude-opus-4-7",
            "usage": {
                "prompt_tokens": 10_000,
                "completion_tokens": 1_000,
                "cache_read_input_tokens": 5_000,
            },
        }
    ]
    result = _format_cost_summary(events)
    # 計算 (★2026-05-29 修正後): prompt_tokens 10K は cache 込み合算 → uncached = 10K-5K = 5K。
    #   5K*$5/1M + 1K*$25/1M + 5K*$0.5/1M = 0.025 + 0.025 + 0.0025 = ~$0.0525 (Opus 4.7 単価)。
    assert "2026-05-23" in result
    assert "$" in result
    assert "claude-opus-4-7" in result


@pytest.mark.smoke
def test_format_cost_summary_prev_day_comparison():
    """2 日分あれば前日比が出る (= rounding で 0.00 にならない量を渡す)。"""
    from bot_metrics import _format_cost_summary
    events = [
        {"event": "turn_finished", "ts": "2026-05-22T10:00:00",
         "model": "gpt-4o", "usage": {"prompt_tokens": 1_000_000, "completion_tokens": 100_000}},
        {"event": "turn_finished", "ts": "2026-05-23T10:00:00",
         "model": "gpt-4o", "usage": {"prompt_tokens": 1_500_000, "completion_tokens": 150_000}},
    ]
    result = _format_cost_summary(events)
    # 前日比 +XX% が出る
    assert "前日比" in result


@pytest.mark.smoke
def test_format_cost_summary_warns_30pct_spike():
    """前日比 +30% 超で警告マークが出る (5/22 base が 0.00 でないことを保証)。"""
    from bot_metrics import _format_cost_summary
    events = [
        # 5/22 base = ~$3.5
        {"event": "turn_finished", "ts": "2026-05-22T10:00:00",
         "model": "gpt-4o", "usage": {"prompt_tokens": 1_000_000, "completion_tokens": 100_000}},
        # 5/23 spike (= base x10)
        {"event": "turn_finished", "ts": "2026-05-23T10:00:00",
         "model": "gpt-4o", "usage": {"prompt_tokens": 10_000_000, "completion_tokens": 1_000_000}},
    ]
    result = _format_cost_summary(events)
    assert "⚠️" in result or "+30%超" in result or "+30%" in result


@pytest.mark.smoke
def test_format_cost_summary_subtracts_cache_write_from_input():
    """cache_creation(cache_write) を input から差し引き、別単価で計上する。

    ★2026-05-29 整合 fix: 旧 bot_metrics は cache_write を読まず prompt_tokens-cache_read
    を input に積んでいた (= cache_write 分を full input price で過大計上 + dashboard と乖離)。
    uncached = pt - cr - cw = 95000-30000-5000 = 60000 になることを固定する。
    """
    from bot_metrics import _format_cost_summary
    events = [
        {"event": "turn_finished", "ts": "2026-05-29T10:00:00",
         "model": "claude-opus-4-7",
         "usage": {"prompt_tokens": 95_000, "completion_tokens": 400,
                   "cache_read_input_tokens": 30_000,
                   "cache_creation_input_tokens": 5_000}},
    ]
    result = _format_cost_summary(events)
    assert "60000t" in result       # uncached = pt - cr - cw
    assert "65000t" not in result   # 旧 bug (cache_write を input に誤計上) なら 65000


@pytest.mark.smoke
def test_format_cost_summary_canonicalizes_dated_model():
    """dated 変種 'gpt-4o-2024-08-06' が 'gpt-4o' へ解決され正しい単価で計上される。

    ★2026-05-29: 旧 bot_metrics は alias/dated を解決せず fallback {$5/$15} に落ちていた。
    canonicalize 後は gpt-4o ($2.5/$10) で 1M in + 0.1M out = $3.50 になる (fallback なら $6.50)。
    """
    from bot_metrics import _format_cost_summary
    events = [
        {"event": "turn_finished", "ts": "2026-05-29T10:00:00",
         "model": "gpt-4o-2024-08-06",
         "usage": {"prompt_tokens": 1_000_000, "completion_tokens": 100_000}},
    ]
    result = _format_cost_summary(events)
    assert "3.50" in result        # gpt-4o 価格で計上 (fallback なら 6.50)
    assert "6.50" not in result


@pytest.mark.smoke
def test_litellm_config_has_max_budget():
    """litellm_config.yaml に max_budget が設定されている (= cost cap 構造防御)。"""
    cfg = (REPO / "litellm_config.yaml").read_text(encoding="utf-8")
    assert "max_budget" in cfg
    assert "budget_duration" in cfg


@pytest.mark.smoke
def test_clone_cost_summary_script_exists():
    """clone_cost_summary.py が存在し、必要 import が揃ってる。"""
    path = REPO / "scripts" / "clone_cost_summary.py"
    assert path.exists()
    txt = path.read_text(encoding="utf-8")
    assert "from bot_metrics import" in txt
    assert "_format_cost_summary" in txt
    assert "line_push" in txt


@pytest.mark.smoke
def test_cron_install_includes_cost_daily():
    """cron_install.sh に cost-daily の cron entry がある (09:00 daily)。"""
    src = (REPO / "scripts" / "cron_install.sh").read_text(encoding="utf-8")
    assert "cost-daily" in src
    assert "0 9 * * *" in src  # 09:00 daily


@pytest.mark.smoke
def test_clone_cron_sh_handles_cost_daily():
    """clone_cron.sh が cost-daily モードを処理する。"""
    src = (REPO / "scripts" / "clone_cron.sh").read_text(encoding="utf-8")
    assert "cost-daily)" in src
    assert "clone_cost_summary.py" in src
