"""smoke test: brain_wiki_helpers/llm_retry.py (★2026-05-22 Phase 3a)。

httpx 依存だが mock 化して network 無しで test。retry policy の核を検証:
- 429 → retry → 成功
- 5xx → retry → 成功
- timeout → retry → 成功
- 4xx (auth) → 即 raise (= retry しない)
- 全 attempt fail → 例外
"""
from __future__ import annotations

import asyncio
from typing import Any

import pytest

httpx_available = True
try:
    import httpx
except Exception:
    httpx_available = False


pytestmark = pytest.mark.smoke


@pytest.fixture
def fake_http():
    """httpx.AsyncClient 互換の最小 mock。post() を script で制御。"""
    if not httpx_available:
        pytest.skip("httpx 未インストール")

    class _FakeResponse:
        def __init__(self, status: int, json_body: dict | None = None):
            self.status_code = status
            self._json = json_body or {}
            self.request = httpx.Request("POST", "http://fake")

        def json(self):
            return self._json

        def raise_for_status(self):
            if self.status_code >= 400:
                raise httpx.HTTPStatusError(
                    f"status {self.status_code}",
                    request=self.request,
                    response=self,
                )

    class _FakeClient:
        def __init__(self, script):
            self.script = list(script)
            self.calls = 0

        async def post(self, url, headers=None, json=None, timeout=None):
            self.calls += 1
            item = self.script.pop(0)
            if isinstance(item, Exception):
                raise item
            return item

    return _FakeResponse, _FakeClient


async def _run(coro):
    return await coro


def test_retry_succeeds_after_429(fake_http):
    """429 → 成功 (2 attempt)。"""
    Resp, Client = fake_http
    ok_payload = {"choices": [{"message": {"content": "hello"}}]}
    http = Client([Resp(429), Resp(200, ok_payload)])
    from brain_wiki_helpers.llm_retry import post_litellm_with_retry

    result = asyncio.run(
        post_litellm_with_retry(
            http, "http://litellm", "key",
            {"model": "smart", "messages": []},
            timeout=5,
            max_retries=3,
            backoff_base=1.01,  # ほぼ無 backoff
        )
    )
    assert result == "hello"
    assert http.calls == 2


def test_retry_succeeds_after_500(fake_http):
    """5xx → 成功 (3 attempt)。"""
    Resp, Client = fake_http
    ok_payload = {"choices": [{"message": {"content": "ok"}}]}
    http = Client([Resp(500), Resp(503), Resp(200, ok_payload)])
    from brain_wiki_helpers.llm_retry import post_litellm_with_retry

    result = asyncio.run(
        post_litellm_with_retry(
            http, "http://litellm", "key",
            {"model": "smart", "messages": []},
            timeout=5,
            max_retries=3,
            backoff_base=1.01,
        )
    )
    assert result == "ok"
    assert http.calls == 3


def test_retry_succeeds_after_timeout(fake_http):
    """timeout → 成功。"""
    Resp, Client = fake_http
    ok_payload = {"choices": [{"message": {"content": "x"}}]}
    http = Client([httpx.TimeoutException("timeout"), Resp(200, ok_payload)])
    from brain_wiki_helpers.llm_retry import post_litellm_with_retry

    result = asyncio.run(
        post_litellm_with_retry(
            http, "http://litellm", "key",
            {"model": "smart", "messages": []},
            timeout=5,
            max_retries=3,
            backoff_base=1.01,
        )
    )
    assert result == "x"


def test_no_retry_on_401(fake_http):
    """4xx (auth error) は即 raise、retry しない。"""
    Resp, Client = fake_http
    http = Client([Resp(401)])
    from brain_wiki_helpers.llm_retry import post_litellm_with_retry

    with pytest.raises(httpx.HTTPStatusError):
        asyncio.run(
            post_litellm_with_retry(
                http, "http://litellm", "key",
                {"model": "smart", "messages": []},
                timeout=5,
                max_retries=3,
                backoff_base=1.01,
            )
        )
    # 1 回だけ
    assert http.calls == 1


def test_exhausts_retries_raises(fake_http):
    """全 attempt fail → 最終 raise。"""
    Resp, Client = fake_http
    http = Client([Resp(429), Resp(429), Resp(429)])
    from brain_wiki_helpers.llm_retry import post_litellm_with_retry

    with pytest.raises(httpx.HTTPStatusError):
        asyncio.run(
            post_litellm_with_retry(
                http, "http://litellm", "key",
                {"model": "smart", "messages": []},
                timeout=5,
                max_retries=3,
                backoff_base=1.01,
            )
        )
    assert http.calls == 3


def test_brain_wiki_method_wraps_helper():
    """brain_wiki.py の _post_litellm_with_retry が helper を呼んでる。"""
    from pathlib import Path
    REPO = Path(__file__).resolve().parent.parent.parent
    src = (REPO / "brain_wiki.py").read_text(encoding="utf-8")
    assert "async def _post_litellm_with_retry(" in src
    assert "from brain_wiki_helpers.llm_retry import post_litellm_with_retry" in src
