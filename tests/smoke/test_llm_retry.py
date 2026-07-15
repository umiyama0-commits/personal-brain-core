"""smoke test: brain_wiki._post_litellm_with_retry (★2026-05-22 海山指示)。

429 / 5xx / network error で exponential backoff retry。
"""
from __future__ import annotations

import asyncio
import re
import sys
import textwrap
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import httpx
import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent.parent


def _extract_retry_method():
    """brain_wiki.py から _post_litellm_with_retry を抽出 + 隔離 exec。

    self.http / self.litellm_url / self.litellm_key を持つ簡易 wrapper を作って
    関数を bound method として動かす。
    """
    src = (REPO_ROOT / "brain_wiki.py").read_text(encoding="utf-8")
    start = src.find("    async def _post_litellm_with_retry(")
    assert start >= 0, "_post_litellm_with_retry not found"
    rest = src[start:]
    # 終端 = 次の def or class (4-space indent)
    end_m = re.search(
        r"\n    def [a-zA-Z_]|\n    async def [a-zA-Z_]|\nclass |\ndef ",
        rest[len("    async def _post_litellm_with_retry("):],
    )
    assert end_m, "end marker not found"
    func_src = rest[: len("    async def _post_litellm_with_retry(") + end_m.start() + 1]
    func_src = textwrap.dedent(func_src)
    # self.method の依存を解決するため、関数を Self stub の bound method として動かす
    return func_src


class _Self:
    """self stub。http / litellm_url / litellm_key を持つ。"""
    def __init__(self, http, litellm_url="http://mock:4000", litellm_key="k"):
        self.http = http
        self.litellm_url = litellm_url
        self.litellm_key = litellm_key


def _make_response(status_code: int, content: str = "ok") -> MagicMock:
    """Mock httpx.Response。"""
    resp = MagicMock()
    resp.status_code = status_code
    resp.request = MagicMock()
    resp.json = MagicMock(return_value={
        "choices": [{"message": {"content": content}}]
    })
    if status_code >= 400:
        resp.raise_for_status = MagicMock(
            side_effect=httpx.HTTPStatusError(
                f"upstream {status_code}", request=resp.request, response=resp
            )
        )
    else:
        resp.raise_for_status = MagicMock()
    return resp


@pytest.fixture
def retry_fn():
    """retry method を bound 関数として返す。"""
    func_src = _extract_retry_method()
    # logger / asyncio / httpx を持つ namespace で exec
    import logging
    ns = {
        "asyncio": asyncio,
        "httpx": httpx,
        "logger": logging.getLogger("test"),
    }
    exec(func_src, ns)
    return ns["_post_litellm_with_retry"]


@pytest.mark.smoke
async def test_retry_429_then_success(retry_fn):
    """429 → 1 回 retry で成功する。"""
    # 1 回目: 429、2 回目: 200
    responses = [_make_response(429), _make_response(200, "hello")]
    call_count = {"n": 0}

    async def fake_post(*args, **kwargs):
        i = call_count["n"]
        call_count["n"] += 1
        return responses[i]

    http = MagicMock()
    http.post = fake_post
    self_ = _Self(http)

    # backoff_base を 1.01 にしてテスト速くする
    result = await retry_fn(self_, {"model": "test", "messages": []}, max_retries=3, backoff_base=1.01)
    assert result == "hello"
    assert call_count["n"] == 2  # 1 回 retry


@pytest.mark.smoke
async def test_retry_5xx_then_success(retry_fn):
    """502 → 1 回 retry で成功。"""
    responses = [_make_response(502), _make_response(200, "ok")]
    call_count = {"n": 0}

    async def fake_post(*args, **kwargs):
        i = call_count["n"]
        call_count["n"] += 1
        return responses[i]

    http = MagicMock()
    http.post = fake_post
    self_ = _Self(http)

    result = await retry_fn(self_, {"model": "test"}, max_retries=3, backoff_base=1.01)
    assert result == "ok"
    assert call_count["n"] == 2


@pytest.mark.smoke
async def test_retry_4xx_no_retry(retry_fn):
    """403/404 等の 4xx (non-429) は **即 raise**、retry しない。"""
    responses = [_make_response(403)]
    call_count = {"n": 0}

    async def fake_post(*args, **kwargs):
        i = call_count["n"]
        call_count["n"] += 1
        return responses[i]

    http = MagicMock()
    http.post = fake_post
    self_ = _Self(http)

    with pytest.raises(httpx.HTTPStatusError):
        await retry_fn(self_, {"model": "test"}, max_retries=3, backoff_base=1.01)
    assert call_count["n"] == 1  # retry なし


@pytest.mark.smoke
async def test_retry_429_exhaustion(retry_fn):
    """429 が連発で max_retries まで使い切ったら raise。"""
    responses = [_make_response(429), _make_response(429), _make_response(429)]
    call_count = {"n": 0}

    async def fake_post(*args, **kwargs):
        i = call_count["n"]
        call_count["n"] += 1
        return responses[i]

    http = MagicMock()
    http.post = fake_post
    self_ = _Self(http)

    with pytest.raises(httpx.HTTPStatusError):
        await retry_fn(self_, {"model": "test"}, max_retries=3, backoff_base=1.01)
    assert call_count["n"] == 3  # 3 回試行


@pytest.mark.smoke
async def test_retry_timeout_then_success(retry_fn):
    """TimeoutException → retry → 成功。"""
    call_count = {"n": 0}

    async def fake_post(*args, **kwargs):
        i = call_count["n"]
        call_count["n"] += 1
        if i == 0:
            raise httpx.TimeoutException("timeout")
        return _make_response(200, "ok")

    http = MagicMock()
    http.post = fake_post
    self_ = _Self(http)

    result = await retry_fn(self_, {"model": "test"}, max_retries=3, backoff_base=1.01)
    assert result == "ok"
    assert call_count["n"] == 2


@pytest.mark.smoke
async def test_retry_success_first_try(retry_fn):
    """初回 200 なら retry しない。"""
    call_count = {"n": 0}

    async def fake_post(*args, **kwargs):
        call_count["n"] += 1
        return _make_response(200, "fresh")

    http = MagicMock()
    http.post = fake_post
    self_ = _Self(http)

    result = await retry_fn(self_, {"model": "test"}, max_retries=3, backoff_base=1.01)
    assert result == "fresh"
    assert call_count["n"] == 1


@pytest.mark.smoke
def test_brain_wiki_callsite_uses_retry():
    """brain_wiki.py 内で clone_respond_public + update_clone_memory が retry 版を使っている。"""
    src = (REPO_ROOT / "brain_wiki.py").read_text(encoding="utf-8")
    # 関数定義
    assert "async def _post_litellm_with_retry(" in src
    # clone_respond_public 内
    assert "self._post_litellm_with_retry(" in src
    # 2 箇所以上で呼ばれてる (= clone_respond + update_clone_memory)
    assert src.count("_post_litellm_with_retry(") >= 3  # 定義 1 + call 2+
