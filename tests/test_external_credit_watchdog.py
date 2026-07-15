"""external_credit_watchdog の残高 check test (ElevenLabs / HeyGen 追加分)。

★2026-06-08 海山指示「各種 API の残高枯渇を自動連絡」。fact-check 済の ElevenLabs / HeyGen
残高 ping の閾値判定と、HeyGen の defensive field 抽出を固定する。
"""
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))

import external_credit_watchdog as w  # noqa: E402


class _Resp:
    def __init__(self, status, payload):
        self.status_code = status
        self._p = payload
        self.text = json.dumps(payload)

    def json(self):
        return self._p


class _Client:
    def __init__(self, payload, status=200):
        self._p, self._s = payload, status

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def get(self, *a, **k):
        return _Resp(self._s, self._p)


def _patch_httpx(monkeypatch, payload, status=200):
    monkeypatch.setattr(w.httpx, "Client", lambda *a, **k: _Client(payload, status))


# ─── HeyGen defensive 抽出 ──────────────────────────

def test_extract_heygen_quota_shapes():
    assert w._extract_heygen_quota({"data": {"remaining_quota": 120}}) == 120.0
    assert w._extract_heygen_quota({"remaining_quota": 55}) == 55.0
    assert w._extract_heygen_quota({"data": {"quota": 30}}) == 30.0
    assert w._extract_heygen_quota({"nope": 1}) is None
    assert w._extract_heygen_quota("x") is None


# ─── skip (key 未設定) ──────────────────────────

def test_elevenlabs_skipped_when_no_key(monkeypatch):
    monkeypatch.setattr(w, "ELEVENLABS_API_KEY", "")
    assert w.check_elevenlabs().get("skipped")


def test_heygen_skipped_when_no_key(monkeypatch):
    monkeypatch.setattr(w, "HEYGEN_API_KEY", "")
    assert w.check_heygen().get("skipped")


# ─── ElevenLabs 閾値判定 ──────────────────────────

def test_elevenlabs_low_triggers_alert(monkeypatch):
    monkeypatch.setattr(w, "ELEVENLABS_API_KEY", "test")
    monkeypatch.setattr(w, "ELEVENLABS_LOW_CHARS", 5000)
    _patch_httpx(monkeypatch, {"character_count": 9800, "character_limit": 10000})  # 残 200
    r = w.check_elevenlabs()
    assert r["ok"] is False
    assert r["remaining_chars"] == 200


def test_elevenlabs_ok_when_plenty(monkeypatch):
    monkeypatch.setattr(w, "ELEVENLABS_API_KEY", "test")
    monkeypatch.setattr(w, "ELEVENLABS_LOW_CHARS", 5000)
    _patch_httpx(monkeypatch, {"character_count": 1000, "character_limit": 100000})  # 残 99000
    r = w.check_elevenlabs()
    assert r["ok"] is True


def test_elevenlabs_http_error_is_degraded_not_alert(monkeypatch):
    monkeypatch.setattr(w, "ELEVENLABS_API_KEY", "test")
    _patch_httpx(monkeypatch, {"detail": "unauthorized"}, status=401)
    r = w.check_elevenlabs()
    assert r["ok"] is True  # degraded は false alarm にしない
    assert "degraded" in r


# ─── HeyGen 閾値判定 + 未知 field ──────────────────────────

def test_heygen_low_triggers_alert(monkeypatch):
    monkeypatch.setattr(w, "HEYGEN_API_KEY", "test")
    monkeypatch.setattr(w, "HEYGEN_LOW_QUOTA", 60.0)
    _patch_httpx(monkeypatch, {"data": {"remaining_quota": 10}})
    r = w.check_heygen()
    assert r["ok"] is False
    assert r["remaining_quota"] == 10.0


def test_heygen_unknown_field_is_degraded_with_raw(monkeypatch):
    monkeypatch.setattr(w, "HEYGEN_API_KEY", "test")
    _patch_httpx(monkeypatch, {"weird": {"shape": 1}})
    r = w.check_heygen()
    assert r["ok"] is True            # 未確定 field は false alarm にしない
    assert "raw" in r                 # 初回確認用に raw を残す
