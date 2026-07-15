"""tests/test_brain_graph_auth.py — Brain Map auth 統一 (key + token 両方 accept)

★2026-05-26 海山指示「brain map が表示されない」:
真因は /api/brain/graph が require_api_key で 401、URL に ?key=... が無いため。
fix: VOICE_ALIGN_TOKEN (= ?token=...) も accept する fallback 追加、dashboard と統一。
"""
from __future__ import annotations

import importlib
import os
import sys
from pathlib import Path
from unittest.mock import MagicMock

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

# fastapi 無い env (= MacBook local) では auth 関連 test を skip
# Mac Studio (docker) では fastapi あり、通る
try:
    import fastapi  # noqa: F401
    _HAS_FASTAPI = True
except ImportError:
    _HAS_FASTAPI = False
needs_fastapi = pytest.mark.skipif(not _HAS_FASTAPI, reason="fastapi 未 install (= local MacBook)")


def _make_request(query_params: dict = None, headers: dict = None):
    """fake Request object。

    ★2026-07-02 P1h: query_params/headers は実 dict をそのまま渡す (.get / __getitem__ を
    既に持つ)。旧実装は real dict の .get を再代入しようとして 'attribute is read-only' で
    全 test が error していた (fastapi skip 環境で一度も走らず bit-rot していた)。
    """
    req = MagicMock()
    req.query_params = query_params or {}
    req.headers = headers or {}
    return req


@needs_fastapi
def test_brain_extension_key_accepted(monkeypatch):
    monkeypatch.setenv("BRAIN_EXTENSION_KEY", "secret-brain-key")
    monkeypatch.setenv("VOICE_ALIGN_TOKEN", "voice-token-xyz")
    if "main" in sys.modules:
        del sys.modules["main"]
    import main  # noqa: E402
    monkeypatch.setattr(main, "BRAIN_EXTENSION_KEY", "secret-brain-key")

    req = _make_request(query_params={"key": "secret-brain-key"})
    result = main.require_api_key(req)
    assert result == "secret-brain-key"


@needs_fastapi
def test_voice_align_token_accepted_as_fallback(monkeypatch):
    """★2026-05-26 海山指示: ?token=VOICE_ALIGN_TOKEN も accept"""
    monkeypatch.setenv("BRAIN_EXTENSION_KEY", "secret-brain-key")
    monkeypatch.setenv("VOICE_ALIGN_TOKEN", "voice-token-xyz")
    if "main" in sys.modules:
        del sys.modules["main"]
    import main
    monkeypatch.setattr(main, "BRAIN_EXTENSION_KEY", "secret-brain-key")

    # key 無し、token あり → OK
    req = _make_request(query_params={"token": "voice-token-xyz"})
    result = main.require_api_key(req)
    assert result == "voice-token-xyz"


@needs_fastapi
def test_wrong_key_rejected(monkeypatch):
    from fastapi import HTTPException
    monkeypatch.setenv("BRAIN_EXTENSION_KEY", "secret-brain-key")
    monkeypatch.setenv("VOICE_ALIGN_TOKEN", "voice-token-xyz")
    if "main" in sys.modules:
        del sys.modules["main"]
    import main
    monkeypatch.setattr(main, "BRAIN_EXTENSION_KEY", "secret-brain-key")

    req = _make_request(query_params={"key": "wrong-key"})
    with pytest.raises(HTTPException) as exc:
        main.require_api_key(req)
    assert exc.value.status_code == 401


@needs_fastapi
def test_wrong_token_rejected(monkeypatch):
    from fastapi import HTTPException
    monkeypatch.setenv("BRAIN_EXTENSION_KEY", "secret-brain-key")
    monkeypatch.setenv("VOICE_ALIGN_TOKEN", "voice-token-xyz")
    if "main" in sys.modules:
        del sys.modules["main"]
    import main
    monkeypatch.setattr(main, "BRAIN_EXTENSION_KEY", "secret-brain-key")

    req = _make_request(query_params={"token": "wrong-token"})
    with pytest.raises(HTTPException) as exc:
        main.require_api_key(req)
    assert exc.value.status_code == 401


@needs_fastapi
def test_no_auth_rejected(monkeypatch):
    from fastapi import HTTPException
    monkeypatch.setenv("BRAIN_EXTENSION_KEY", "secret-brain-key")
    monkeypatch.setenv("VOICE_ALIGN_TOKEN", "voice-token-xyz")
    if "main" in sys.modules:
        del sys.modules["main"]
    import main
    monkeypatch.setattr(main, "BRAIN_EXTENSION_KEY", "secret-brain-key")

    req = _make_request(query_params={})
    with pytest.raises(HTTPException) as exc:
        main.require_api_key(req)
    assert exc.value.status_code == 401


@needs_fastapi
def test_authorization_header_accepted(monkeypatch):
    monkeypatch.setenv("BRAIN_EXTENSION_KEY", "secret-brain-key")
    if "main" in sys.modules:
        del sys.modules["main"]
    import main
    monkeypatch.setattr(main, "BRAIN_EXTENSION_KEY", "secret-brain-key")

    req = _make_request(headers={"Authorization": "Bearer secret-brain-key"})
    result = main.require_api_key(req)
    assert result == "secret-brain-key"


def test_brain_graph_html_includes_token_param():
    """brain_graph.py SPA が ?token= も読むよう更新されてる"""
    src = (REPO_ROOT / "brain_graph.py").read_text()
    assert "qs.get('token')" in src or "qs.get(\"token\")" in src
    # buildApiUrl で token を 引き継ぐ
    assert "params.set('token'" in src or 'params.set("token"' in src
