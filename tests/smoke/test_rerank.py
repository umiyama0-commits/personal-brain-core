"""smoke test: brain_wiki_helpers/rerank.py (★2026-05-24 Plan C v2 Step 2)

Cohere Rerank 3.5 統合の構造 sanity + graceful degradation test。
実 API call は行わない (= mock 経由)。
"""
from __future__ import annotations

import os
import sys
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest

REPO = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(REPO))


@pytest.mark.smoke
def test_module_imports():
    """rerank module 読込可能。"""
    from brain_wiki_helpers import rerank
    assert hasattr(rerank, "cohere_rerank")
    assert hasattr(rerank, "estimate_cost")
    # ★fact-checker verify: rerank-v3.5 (= multilingual-v3.5 は存在しない)
    assert rerank.COHERE_RERANK_MODEL == "rerank-v3.5"
    assert rerank.COHERE_API_URL == "https://api.cohere.com/v2/rerank"


@pytest.mark.smoke
async def test_skip_without_api_key(monkeypatch):
    """COHERE_API_KEY 未設定 → None 返却 (= graceful degradation、retrieval pipeline 動作維持)。"""
    monkeypatch.delenv("COHERE_API_KEY", raising=False)
    from brain_wiki_helpers.rerank import cohere_rerank
    http_mock = AsyncMock()
    result = await cohere_rerank(
        query="test", documents=["doc1", "doc2"],
        http=http_mock,
    )
    assert result is None
    # API call は走らない
    http_mock.post.assert_not_called()


@pytest.mark.smoke
async def test_empty_query_or_documents(monkeypatch):
    """query / documents 空 → None 返却。"""
    monkeypatch.setenv("COHERE_API_KEY", "test_key")
    from brain_wiki_helpers.rerank import cohere_rerank
    http_mock = AsyncMock()
    assert await cohere_rerank(query="", documents=["d"], http=http_mock) is None
    assert await cohere_rerank(query="q", documents=[], http=http_mock) is None


@pytest.mark.smoke
async def test_success_path(monkeypatch):
    """正常系: Cohere response から results 抽出。"""
    monkeypatch.setenv("COHERE_API_KEY", "test_key")
    from brain_wiki_helpers.rerank import cohere_rerank

    fake_resp = MagicMock()
    fake_resp.raise_for_status = MagicMock()
    fake_resp.json = MagicMock(return_value={
        "results": [
            {"index": 2, "relevance_score": 0.95},
            {"index": 0, "relevance_score": 0.87},
            {"index": 1, "relevance_score": 0.42},
        ],
        "meta": {"billed_units": {"search_units": 1}},
    })
    http_mock = AsyncMock()
    http_mock.post = AsyncMock(return_value=fake_resp)

    result = await cohere_rerank(
        query="客単価教えて",
        documents=["doc 0", "doc 1", "doc 2"],
        http=http_mock,
        top_n=3,
    )
    assert result is not None
    assert len(result) == 3
    assert result[0]["index"] == 2
    assert result[0]["relevance_score"] == 0.95


@pytest.mark.smoke
async def test_api_failure_fallback(monkeypatch):
    """API 接続失敗 → None 返却 (= caller fallback)。"""
    monkeypatch.setenv("COHERE_API_KEY", "test_key")
    from brain_wiki_helpers.rerank import cohere_rerank

    http_mock = AsyncMock()
    http_mock.post = AsyncMock(side_effect=Exception("network error"))

    result = await cohere_rerank(
        query="test", documents=["d1", "d2"],
        http=http_mock,
    )
    assert result is None


@pytest.mark.smoke
async def test_request_body_structure(monkeypatch):
    """request body が fact-checker verify 通り (= return_documents False、max_tokens_per_doc 4096)。"""
    monkeypatch.setenv("COHERE_API_KEY", "test_key")
    from brain_wiki_helpers.rerank import cohere_rerank

    captured: dict = {}

    async def fake_post(url, headers=None, json=None, timeout=None):
        captured["url"] = url
        captured["headers"] = headers
        captured["json"] = json
        resp = MagicMock()
        resp.raise_for_status = MagicMock()
        resp.json = MagicMock(return_value={"results": []})
        return resp

    http_mock = AsyncMock()
    http_mock.post = fake_post

    await cohere_rerank(
        query="q", documents=["d1", "d2"], http=http_mock, top_n=5,
    )

    # URL 確認
    assert captured["url"] == "https://api.cohere.com/v2/rerank"
    # Authorization Bearer (大文字)
    assert captured["headers"]["Authorization"].startswith("Bearer ")
    # body fields
    body = captured["json"]
    assert body["model"] == "rerank-v3.5"
    assert body["query"] == "q"
    assert body["documents"] == ["d1", "d2"]
    assert body["top_n"] == 2  # min(top_n=5, len(documents)=2)
    assert body["return_documents"] is False  # 軽量化
    assert body["max_tokens_per_doc"] == 4096


@pytest.mark.smoke
def test_estimate_cost():
    """cost 試算 sanity。"""
    from brain_wiki_helpers.rerank import estimate_cost
    c1 = estimate_cost(1500)   # 1500 query/月 = $3/月
    c2 = estimate_cost(6000)   # 6000 query/月 = $12/月
    assert c1["usd_per_month"] == 3.0
    assert c2["usd_per_month"] == 12.0
    assert c1["usd_per_query"] == 0.002
