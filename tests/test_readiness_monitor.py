"""bot_uptime_monitor.check_readiness の test (/ready 監視接続)。

★2026-06-08 評価 SRE: /ready を監視に接続。transient blip を再 check で除外し、依存異常時のみ
not-ready 判定 (restart はしない=alert のみ)。
"""
import sys
import types
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))

import bot_uptime_monitor as m  # noqa: E402


class _R:
    def __init__(self, code, body):
        self.status_code = code
        self._b = body

    def json(self):
        return self._b


def _patch(monkeypatch, responses):
    """responses = list of _R を順に返す httpx mock。time.sleep を潰す。"""
    seq = list(responses)
    monkeypatch.setattr(m, "httpx", types.SimpleNamespace(get=lambda *a, **k: seq.pop(0)))
    import time as _t
    monkeypatch.setattr(_t, "sleep", lambda s: None)


def test_ready_200_ok(monkeypatch):
    _patch(monkeypatch, [_R(200, {"ready": True})])
    r = m.check_readiness()
    assert r["ok"] is True


def test_sustained_503_not_ready_with_failed_deps(monkeypatch):
    body = {"ready": False, "checks": {"redis": True, "chromadb": False, "litellm": True}}
    _patch(monkeypatch, [_R(503, body), _R(503, body)])  # 2 連続失敗
    r = m.check_readiness()
    assert r["ok"] is False
    assert r["failed_deps"] == ["chromadb"]


def test_transient_blip_recovers_on_recheck(monkeypatch):
    _patch(monkeypatch, [_R(503, {"checks": {}}), _R(200, {})])  # 1回目NG→再checkOK
    r = m.check_readiness()
    assert r["ok"] is True
    assert r.get("note") == "recovered on recheck"


def test_connection_error_then_fail(monkeypatch):
    # httpx.get が例外 → _probe が (None, error) → 再 check も失敗
    def _boom(*a, **k):
        raise RuntimeError("conn refused")
    monkeypatch.setattr(m, "httpx", types.SimpleNamespace(get=_boom))
    import time as _t
    monkeypatch.setattr(_t, "sleep", lambda s: None)
    r = m.check_readiness()
    assert r["ok"] is False


# ─── ★auth_denied burst (brute-force 検知) ──────────────────────────

def test_auth_denied_burst_detected(monkeypatch):
    evs = [{"event": "auth_denied", "action": "deploy_token", "token_id": f"t{i}"} for i in range(12)]
    monkeypatch.setattr(m, "iter_events", lambda since_sec=None: evs)
    r = m.check_auth_denied_burst(threshold=10)
    assert r["is_burst"] is True
    assert r["n_denied"] == 12
    assert r["distinct_tokens"] == 12


def test_auth_denied_few_not_burst(monkeypatch):
    evs = [{"event": "auth_denied", "action": "view_token", "token_id": "x"}] * 3
    monkeypatch.setattr(m, "iter_events", lambda since_sec=None: evs)
    assert m.check_auth_denied_burst(threshold=10)["is_burst"] is False
