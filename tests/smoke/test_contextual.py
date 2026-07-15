"""smoke test: brain_wiki_helpers/contextual.py (★2026-05-23 Plan C v2 Step 1)

Anthropic Contextual Retrieval の構造 sanity + 失敗時 fallback test。
実 API call は行わない (= mock 経由)。
"""
from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest

REPO = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(REPO))


@pytest.mark.smoke
def test_module_imports():
    """contextual module が import 可能。"""
    from brain_wiki_helpers import contextual
    assert hasattr(contextual, "contextualize_chunks")
    assert hasattr(contextual, "estimate_cost")
    assert contextual.MIN_DOCUMENT_CHARS_FOR_CONTEXT == 4000


@pytest.mark.smoke
async def test_skip_short_document():
    """document が threshold 未満 → context generation skip、元 chunk 返却。"""
    from brain_wiki_helpers.contextual import contextualize_chunks

    short_doc = "short document " * 50  # ~750 char、threshold 4000 未満
    chunks = ["chunk 1", "chunk 2", "chunk 3"]
    http_mock = AsyncMock()

    result, stats = await contextualize_chunks(
        document_text=short_doc,
        chunks=chunks,
        http=http_mock,
        litellm_url="http://test",
        litellm_key="test_key",
    )
    assert result == chunks  # 元 chunk のまま
    assert stats["skipped_short_doc"] is True
    assert stats["n_succeed"] == 0
    assert stats["n_failed"] == 0
    # HTTP call も発生しない (= cost 0)
    http_mock.post.assert_not_called()


@pytest.mark.smoke
async def test_contextualize_success_path():
    """正常系: 各 chunk に context prefix 付与される。"""
    from brain_wiki_helpers.contextual import contextualize_chunks

    long_doc = "本文 " * 2000  # ~6000 char、threshold 越え
    chunks = ["chunk A", "chunk B"]

    # mock httpx.AsyncClient response (= LiteLLM JSON 形式)
    fake_response = MagicMock()
    fake_response.raise_for_status = MagicMock()
    fake_response.json = MagicMock(
        return_value={
            "choices": [{"message": {"content": "Generated context for this chunk."}}]
        }
    )
    http_mock = AsyncMock()
    http_mock.post = AsyncMock(return_value=fake_response)

    result, stats = await contextualize_chunks(
        document_text=long_doc,
        chunks=chunks,
        http=http_mock,
        litellm_url="http://test",
        litellm_key="test_key",
    )
    assert len(result) == 2
    for r in result:
        assert r.startswith("[Context: Generated context for this chunk.]")
    assert stats["n_succeed"] == 2
    assert stats["n_failed"] == 0
    # 2 chunks → 2 calls
    assert http_mock.post.call_count == 2


@pytest.mark.smoke
async def test_contextualize_failure_fallback():
    """API 失敗時、元 chunk が返る + n_failed カウント。"""
    from brain_wiki_helpers.contextual import contextualize_chunks

    long_doc = "本文 " * 2000
    chunks = ["chunk X"]

    http_mock = AsyncMock()
    http_mock.post = AsyncMock(side_effect=Exception("api fail"))

    result, stats = await contextualize_chunks(
        document_text=long_doc,
        chunks=chunks,
        http=http_mock,
        litellm_url="http://test",
        litellm_key="test_key",
    )
    # 失敗時 fallback = 元 chunk のまま
    assert result == chunks
    assert stats["n_failed"] == 1
    assert stats["n_succeed"] == 0


@pytest.mark.smoke
def test_estimate_cost_cached():
    """cost 試算 helper の sanity (cached / uncached の差を確認)。"""
    from brain_wiki_helpers.contextual import estimate_cost

    cached = estimate_cost(n_chunks=290, avg_doc_chars=4000, cached=True)
    uncached = estimate_cost(n_chunks=290, avg_doc_chars=4000, cached=False)

    # cached が確実に安い
    assert cached["total_usd"] < uncached["total_usd"]
    # USD として妥当な範囲 (= cookbook 試算ベース)
    assert 0.05 < cached["total_usd"] < 10.0
    assert 0.1 < uncached["total_usd"] < 50.0


@pytest.mark.smoke
def test_strip_context_prefix_removes_baked_in_marker():
    """★Adversary 指摘 fix の検証: chunk doc に baked-in した [Context: ...] が剥がれる。"""
    from brain_wiki_helpers.contextual import strip_context_prefix

    # Contextual Retrieval で prepend された chunk
    chunk_with_ctx = (
        "[Context: 2026-05-21 役員会で海山が経営戦略議論時に発言]\n\n"
        "日本市場でのシェア拡大を最優先とする"
    )
    result = strip_context_prefix(chunk_with_ctx)
    assert "[Context:" not in result
    assert result.startswith("日本市場でのシェア拡大を最優先とする")


@pytest.mark.smoke
def test_strip_context_prefix_idempotent_on_clean_chunk():
    """prefix 無い chunk に対しても安全 (= 何も変えない)。"""
    from brain_wiki_helpers.contextual import strip_context_prefix

    clean = "ROI 2 年で見たい。\n\n面・線・点の 3 点を見る。"
    assert strip_context_prefix(clean) == clean


@pytest.mark.smoke
def test_strip_context_prefix_only_first_match():
    """[Context:] が本文中に出てきても、最初の prefix だけ剥がす (= 本文中の偶発 match 保護)。"""
    from brain_wiki_helpers.contextual import strip_context_prefix

    # 1 回目 prefix + 本文中に [Context: ...] (= 引用 / メタ言及) を残す
    chunk = (
        "[Context: meeting note]\n\n"
        "発言の中で 「[Context: ...]」と書く場合は残してほしい。"
    )
    result = strip_context_prefix(chunk)
    assert result.startswith("発言の中で")
    # 本文中の [Context:] は残る
    assert "[Context: ...]" in result


@pytest.mark.smoke
def test_cache_control_structure():
    """API call の content block 構造が cookbook 準拠か (= cache_control が document block のみ)。"""
    import inspect
    from brain_wiki_helpers import contextual

    src = inspect.getsource(contextual.contextualize_chunks)
    # cache_control は document block にのみ
    assert "cache_control" in src
    assert '"type": "ephemeral"' in src
    assert '"ttl": "1h"' in src
    # document block 構造 (= source code 内に "<document>" タグが f-string で構築されてる)
    assert "<document>" in src
    # chunk prompt は module-level 定数 _CHUNK_PROMPT に
    assert "<chunk>" in contextual._CHUNK_PROMPT
    assert "<chunk>" in contextual._CHUNK_PROMPT  # cookbook フォーマット
