"""smoke test: external_credit_watchdog の Vapi 通話検知 + LiteLLM spend (★2026-06-07 エージェント評価)

旧コードは Vapi の実在しない残高 endpoint を叩き silent、LiteLLM は非標準 /spend を使っていた。
WebSearch で検証した実在 API (Vapi GET /call / LiteLLM /global/spend/report) への修正の回帰保護。
"""
from __future__ import annotations

import sys
from datetime import datetime, timezone, timedelta
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(REPO / "scripts"))


class _Resp:
    def __init__(self, code, payload):
        self.status_code = code
        self._p = payload
        self.text = str(payload)

    def json(self):
        return self._p


def _client(payload, code=200):
    class _C:
        def __enter__(self_):
            return self_

        def __exit__(self_, *a):
            pass

        def get(self_, *a, **k):
            return _Resp(code, payload)
    return lambda *a, **k: _C()


@pytest.fixture
def w(monkeypatch):
    monkeypatch.setenv("VAPI_PRIVATE_API_KEY", "x")
    monkeypatch.setenv("LITELLM_MASTER_KEY", "x")
    import importlib
    import external_credit_watchdog as mod
    importlib.reload(mod)
    return mod


def _ts(hours_ago):
    return (datetime.now(timezone.utc) - timedelta(hours=hours_ago)).strftime("%Y-%m-%dT%H:%M:%SZ")


@pytest.mark.smoke
def test_vapi_recent_calls_ok(w, monkeypatch):
    monkeypatch.setattr(w.httpx, "Client", _client([{"createdAt": _ts(1)}, {"createdAt": _ts(2)}]))
    r = w.check_vapi()
    assert r["ok"] is True and r["n_calls_window"] == 2


@pytest.mark.smoke
def test_vapi_zero_recent_calls_alerts(w, monkeypatch):
    """直近 window 内に通話 0 件 → ok:False (credit-out / 起動失敗 proxy)。"""
    monkeypatch.setattr(w.httpx, "Client", _client([{"createdAt": _ts(100)}]))  # 全て window 外
    r = w.check_vapi()
    assert r["ok"] is False and r["n_calls_window"] == 0 and r["note"]


@pytest.mark.smoke
def test_vapi_api_error_degrades_no_false_alarm(w, monkeypatch):
    """GET /call が失敗しても watchdog 自身の故障で誤警報しない (ok:True + degraded)。"""
    monkeypatch.setattr(w.httpx, "Client", _client({}, code=500))
    r = w.check_vapi()
    assert r["ok"] is True and "degraded" in r


@pytest.mark.smoke
def test_litellm_spend_over_threshold_alerts(w, monkeypatch):
    """/global/spend/report の spend が予算の 80% 超 → ok:False。"""
    monkeypatch.setattr(w.httpx, "Client", _client([{"total_spend": 45.0}]))  # 45/50 = 90%
    monkeypatch.setattr(w, "LITELLM_MAX_BUDGET", 50.0)
    r = w.check_litellm()
    assert r["ok"] is False and r["used_usd"] == 45.0 and r["usage_pct"] == 90.0


@pytest.mark.smoke
def test_litellm_spend_under_threshold_ok(w, monkeypatch):
    monkeypatch.setattr(w.httpx, "Client", _client([{"total_spend": 10.0}]))  # 10/50 = 20%
    monkeypatch.setattr(w, "LITELLM_MAX_BUDGET", 50.0)
    r = w.check_litellm()
    assert r["ok"] is True


@pytest.mark.smoke
def test_sum_litellm_spend_no_double_count(w):
    assert w._sum_litellm_spend([{"total_spend": 3.0}, {"total_spend": 1.5}]) == 4.5
    assert w._sum_litellm_spend([{"total_spend": 5.0, "nested": {"spend": 99}}]) == 5.0
