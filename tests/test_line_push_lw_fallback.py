"""line_push の LINE Works fallback + 日次cap (★2026-06-11、★2026-07-10 critical 限定化)。

personal LINE の無料枠 (200通/月) 枯渇で全通知が 6日間 silent fail した事故の再発防止
+ ★2026-07-10 海山「LINE WORKS はあくまで社員公開用」:
1. 非200 を握りつぶさない (log + 挙動分岐)
2. LW DM 迂回 (ADMIN_LW_USER_ID 宛) は **critical=True のみ** — info/warning/レポートは
   personal 限定で、quota 超過・失敗時は drop (LW に流さない)
3. critical は日次 cap をバイパスして personal を先に試す (LW は最終手段)
4. LW_FALLBACK_DISABLE=1 で critical でも LW 遮断
5. ADMIN_LW_USER_ID 未設定なら loud-skip で False
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))

import clone_improve_lib as lib  # noqa: E402


class _FakeResp:
    def __init__(self, status_code: int, text: str = "", payload: dict | None = None):
        self.status_code = status_code
        self.text = text
        self._payload = payload or {}

    def json(self):
        return self._payload

    def raise_for_status(self):
        if self.status_code >= 400:
            raise RuntimeError(f"HTTP {self.status_code}")


class _FakeClient:
    """httpx.Client の代替。post は固定 status を返す。"""
    status = 200

    def __init__(self, *a, **k):
        pass

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def post(self, *a, **k):
        return _FakeResp(self.status, '{"message":"quota"}')


def _spy_lw(monkeypatch, calls: list, result: bool = True):
    """_lw_admin_push を記録 spy に差し替え (routing 検証用)。"""
    def fake(text: str) -> bool:
        calls.append(text)
        return result
    monkeypatch.setattr(lib, "_lw_admin_push", fake)


def _no_cap(monkeypatch, tmp_path):
    monkeypatch.setattr(lib, "_LINE_PUSH_STATE", tmp_path / "state.json")
    monkeypatch.setattr(lib, "NOTIFY_DIGEST_QUEUE", tmp_path / "digest.jsonl")
    monkeypatch.delenv("LINE_PUSH_DAILY_CAP", raising=False)
    monkeypatch.delenv("LW_FALLBACK_DISABLE", raising=False)


# ─── routing ───────────────────────────────────────────────
def test_personal_200_returns_true_without_fallback(monkeypatch, tmp_path):
    monkeypatch.setenv("LINE_CHANNEL_ACCESS_TOKEN", "tok")
    monkeypatch.setenv("ALIGNMENT_TARGET_USER", "U1")
    _no_cap(monkeypatch, tmp_path)
    _FakeClient.status = 200
    monkeypatch.setattr(lib.httpx, "Client", _FakeClient)
    calls: list = []
    _spy_lw(monkeypatch, calls)
    assert lib.line_push("hello") is True
    assert calls == []  # 200 なら fallback は呼ばれない


def test_critical_429_falls_back_to_lw(monkeypatch, tmp_path):
    # critical: quota 枯渇 (429) → LW DM に迂回して True
    monkeypatch.setenv("LINE_CHANNEL_ACCESS_TOKEN", "tok")
    monkeypatch.setenv("ALIGNMENT_TARGET_USER", "U1")
    _no_cap(monkeypatch, tmp_path)
    _FakeClient.status = 429
    monkeypatch.setattr(lib.httpx, "Client", _FakeClient)
    calls: list = []
    _spy_lw(monkeypatch, calls)
    assert lib.line_push("alert!", critical=True) is True
    assert calls == ["alert!"]


def test_noncritical_429_drops_without_lw(monkeypatch, tmp_path):
    # ★2026-07-10: 非critical は 429 でも LW に流さず drop (False)
    monkeypatch.setenv("LINE_CHANNEL_ACCESS_TOKEN", "tok")
    monkeypatch.setenv("ALIGNMENT_TARGET_USER", "U1")
    _no_cap(monkeypatch, tmp_path)
    _FakeClient.status = 429
    monkeypatch.setattr(lib.httpx, "Client", _FakeClient)
    calls: list = []
    _spy_lw(monkeypatch, calls)
    assert lib.line_push("info notice") is False
    assert calls == []


def test_lw_fallback_disable_blocks_even_critical(monkeypatch, tmp_path):
    # LW_FALLBACK_DISABLE=1 → critical + 429 でも LW に流さない
    monkeypatch.setenv("LINE_CHANNEL_ACCESS_TOKEN", "tok")
    monkeypatch.setenv("ALIGNMENT_TARGET_USER", "U1")
    _no_cap(monkeypatch, tmp_path)
    monkeypatch.setenv("LW_FALLBACK_DISABLE", "1")
    _FakeClient.status = 429
    monkeypatch.setattr(lib.httpx, "Client", _FakeClient)
    calls: list = []
    _spy_lw(monkeypatch, calls)
    assert lib.line_push("alert!", critical=True) is False
    assert calls == []


def test_missing_personal_env_critical_goes_to_lw(monkeypatch, tmp_path):
    monkeypatch.delenv("LINE_CHANNEL_ACCESS_TOKEN", raising=False)
    monkeypatch.delenv("ALIGNMENT_TARGET_USER", raising=False)
    _no_cap(monkeypatch, tmp_path)
    calls: list = []
    _spy_lw(monkeypatch, calls)
    assert lib.line_push("direct", critical=True) is True
    assert calls == ["direct"]


def test_missing_personal_env_noncritical_drops(monkeypatch, tmp_path):
    monkeypatch.delenv("LINE_CHANNEL_ACCESS_TOKEN", raising=False)
    monkeypatch.delenv("ALIGNMENT_TARGET_USER", raising=False)
    _no_cap(monkeypatch, tmp_path)
    calls: list = []
    _spy_lw(monkeypatch, calls)
    assert lib.line_push("report") is False
    assert calls == []


def test_fallback_without_admin_id_is_false(monkeypatch, tmp_path):
    # 実物 _lw_admin_push: ADMIN_LW_USER_ID 未設定 → loud-skip False
    monkeypatch.setenv("LINE_CHANNEL_ACCESS_TOKEN", "tok")
    monkeypatch.setenv("ALIGNMENT_TARGET_USER", "U1")
    monkeypatch.delenv("ADMIN_LW_USER_ID", raising=False)
    _no_cap(monkeypatch, tmp_path)
    _FakeClient.status = 429
    monkeypatch.setattr(lib.httpx, "Client", _FakeClient)
    monkeypatch.delenv("LW_FALLBACK_DISABLE", raising=False)
    assert lib.line_push("alert!", critical=True) is False


# ─── 日次 cap ──────────────────────────────────────────────
def test_daily_cap_diverts_noncritical_overflow(monkeypatch, tmp_path):
    """★2026-07-10: cap 超の非critical は LW に流さない。
    ★2026-08-03: 落とし先は drop → digest queue (内容は保全、追加コストは実質ゼロ)。"""
    monkeypatch.setenv("LINE_CHANNEL_ACCESS_TOKEN", "tok")
    monkeypatch.setenv("ALIGNMENT_TARGET_USER", "U1")
    monkeypatch.setenv("LINE_PUSH_DAILY_CAP", "2")
    monkeypatch.setattr(lib, "_LINE_PUSH_STATE", tmp_path / "state.json")
    monkeypatch.setattr(lib, "NOTIFY_DIGEST_QUEUE", tmp_path / "digest.jsonl")
    # ★2026-08-17: _daily_cap は 2026-08-03 から月枠の実残量で動的に決まるようになり、
    # LINE_PUSH_DAILY_CAP は「state が無い時の fallback」に格下げされた。この state を
    # 隔離しないと **本番の残量次第でテストが赤になる** (実際 171/200 消費時に cap=2 と
    # 算出され、cap=1 前提の本 test が落ちた)。tmp に逃がして env の値を効かせる。
    monkeypatch.setattr(lib, "_LINE_QUOTA_STATE", tmp_path / "quota_month.json")
    monkeypatch.delenv("LW_FALLBACK_DISABLE", raising=False)
    _FakeClient.status = 200

    personal_calls = {"n": 0}

    class _Counting(_FakeClient):
        def post(self, *a, **k):
            personal_calls["n"] += 1
            return _FakeResp(self.status)

    monkeypatch.setattr(lib.httpx, "Client", _Counting)
    lw_calls: list = []
    _spy_lw(monkeypatch, lw_calls)

    assert lib.line_push("1") is True    # personal 1通目
    assert lib.line_push("2") is True    # personal 2通目 (cap=2 到達)
    assert lib.line_push("3") is True    # 3通目 → digest 回送 (LW には流さない)
    assert personal_calls["n"] == 2, "回送分が即時 push に漏れている"
    assert lw_calls == []
    assert "3" in lib.NOTIFY_DIGEST_QUEUE.read_text(encoding="utf-8")


def test_critical_bypasses_daily_cap(monkeypatch, tmp_path):
    """critical は日次 cap をバイパスして personal を先に試す (LW 回避優先)。"""
    monkeypatch.setenv("LINE_CHANNEL_ACCESS_TOKEN", "tok")
    monkeypatch.setenv("ALIGNMENT_TARGET_USER", "U1")
    monkeypatch.setenv("LINE_PUSH_DAILY_CAP", "1")
    monkeypatch.setattr(lib, "_LINE_PUSH_STATE", tmp_path / "state.json")
    monkeypatch.setattr(lib, "NOTIFY_DIGEST_QUEUE", tmp_path / "digest.jsonl")
    # ★2026-08-17: _daily_cap は 2026-08-03 から月枠の実残量で動的に決まるようになり、
    # LINE_PUSH_DAILY_CAP は「state が無い時の fallback」に格下げされた。この state を
    # 隔離しないと **本番の残量次第でテストが赤になる** (実際 171/200 消費時に cap=2 と
    # 算出され、cap=1 前提の本 test が落ちた)。tmp に逃がして env の値を効かせる。
    monkeypatch.setattr(lib, "_LINE_QUOTA_STATE", tmp_path / "quota_month.json")
    monkeypatch.delenv("LW_FALLBACK_DISABLE", raising=False)
    _FakeClient.status = 200

    personal_calls = {"n": 0}

    class _Counting(_FakeClient):
        def post(self, *a, **k):
            personal_calls["n"] += 1
            return _FakeResp(self.status)

    monkeypatch.setattr(lib.httpx, "Client", _Counting)
    lw_calls: list = []
    _spy_lw(monkeypatch, lw_calls)

    assert lib.line_push("1") is True                     # cap=1 到達
    # ★2026-08-03: 上限超の非 critical は drop ではなく digest へ回送 (欠落ゼロ)。
    # 本 test の不変条件は「LW には流さない」であり、それは維持される。
    assert lib.line_push("2") is True                     # 非critical → digest 回送
    assert lib.line_push("dead!", critical=True) is True  # critical → cap 無視で personal
    assert personal_calls["n"] == 2, "digest 回送分が即時 push に漏れている"
    assert lw_calls == []
    assert "2" in lib.NOTIFY_DIGEST_QUEUE.read_text(encoding="utf-8")


def test_cap_zero_disables(monkeypatch, tmp_path):
    monkeypatch.setenv("LINE_CHANNEL_ACCESS_TOKEN", "tok")
    monkeypatch.setenv("ALIGNMENT_TARGET_USER", "U1")
    monkeypatch.setenv("LINE_PUSH_DAILY_CAP", "0")
    monkeypatch.setattr(lib, "_LINE_PUSH_STATE", tmp_path / "state.json")
    monkeypatch.setattr(lib, "NOTIFY_DIGEST_QUEUE", tmp_path / "digest.jsonl")
    _FakeClient.status = 200
    monkeypatch.setattr(lib.httpx, "Client", _FakeClient)
    for i in range(20):
        assert lib.line_push(f"m{i}") is True  # 0 = 無効、全部 personal


# ─── _lw_admin_push 内部 (自己完結 RS256 実装) ─────────────────
def test_lw_admin_push_jwt_and_message_post(monkeypatch):
    """生成した RSA 鍵で JWT を構築し、token→message の 2 POST が正しい形か。"""
    pytest.importorskip("cryptography")   # CI 最小依存には無い → skip(本番/dev は導入済)
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric import rsa

    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    pem = key.private_bytes(
        serialization.Encoding.PEM,
        serialization.PrivateFormat.PKCS8,
        serialization.NoEncryption(),
    ).decode()

    monkeypatch.setenv("ADMIN_LW_USER_ID", "lw-umiyama")
    monkeypatch.setenv("LW_CLIENT_ID", "cid")
    monkeypatch.setenv("LW_CLIENT_SECRET", "csec")
    monkeypatch.setenv("LW_SERVICE_ACCOUNT", "sa@dom")
    monkeypatch.setenv("LW_BOT_ID", "bot1")
    monkeypatch.delenv("LW_PRIVATE_KEY_PATH", raising=False)
    monkeypatch.setenv("LW_PRIVATE_KEY", pem)

    posts: list = []

    class _LWClient:
        def __init__(self, *a, **k):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def post(self, url, **kw):
            posts.append((url, kw))
            if "oauth2" in url:
                return _FakeResp(200, payload={"access_token": "AT"})
            return _FakeResp(201)

    monkeypatch.setattr(lib.httpx, "Client", _LWClient)
    assert lib._lw_admin_push("hello world") is True
    assert len(posts) == 2
    # 1st: token 要求 (JWT assertion は 3 セグメント)
    tok_url, tok_kw = posts[0]
    assert "auth.worksmobile.com" in tok_url
    assertion = tok_kw["data"]["assertion"]
    assert assertion.count(".") == 2
    import base64
    hdr = json.loads(base64.urlsafe_b64decode(assertion.split(".")[0] + "=="))
    assert hdr["alg"] == "RS256"
    body = json.loads(base64.urlsafe_b64decode(assertion.split(".")[1] + "=="))
    assert body["iss"] == "cid" and body["sub"] == "sa@dom"
    # 2nd: message POST (宛先 + 📟 prefix)
    msg_url, msg_kw = posts[1]
    assert "/bots/bot1/users/lw-umiyama/messages" in msg_url
    assert msg_kw["json"]["content"]["text"].startswith("📟 [system]")
    assert "hello world" in msg_kw["json"]["content"]["text"]
    assert msg_kw["headers"]["Authorization"] == "Bearer AT"
