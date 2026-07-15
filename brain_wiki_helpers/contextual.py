"""Contextual Retrieval helper (★2026-05-23 Plan C v2 Step 1、海山 Step 1 OK).

Anthropic 公式 cookbook 準拠の Contextual Retrieval 実装。
各 chunk に対し 50-100 token の context を LLM で生成し、chunk に prepend してから
embed + BM25 index 化することで retrieval failure を -67% 削減する手法 (公式実証)。

公式仕様 (= verification subagent で確認済):
- user message に 2 content block (= document + chunk 質問)、system message **使わない**
- cache_control: ephemeral + ttl: "1h" を document block に付与
- min cache token 閾値: Haiku 4.5 = 4,096 tok (= 約 8000 char)、未満は cache 効かない
- N chunks 連続 call で同じ document を cache 経由再利用 (= 90% off)

cookbook 出典:
- https://platform.claude.com/cookbook/capabilities-contextual-embeddings-guide
- https://www.anthropic.com/news/contextual-retrieval

cost 試算 (= subagent verify):
- Haiku 4.5: wiki 平均 4k tok × 17 chunks / file × 17 files (= 290 chunks) で
  cached **$2-3**、uncached $9-10。Phase 1 (wiki/meetings/) なら ~$0.5。

★ Adversary agent 指摘 (2026-05-23): reindex 並行 risk
- ENABLE_CONTEXTUAL_RETRIEVAL=true + 全 wiki reindex を line-bot 稼働中に走らせると
  chromadb 並行アクセス (= CLAUDE.md 1.5) に抵触、SIGSEGV crash loop の可能性。
- **必ず line-bot stop してから reindex 実行**:
    docker compose stop line-bot
    docker run --rm -v ...  python3 -c "...reindex..."
    docker compose start line-bot
- 段階 rollout: Phase 1 は wiki/meetings/ 限定 (= 17 file × ~5s = 1-2 分で完了、
  並行 risk 窓は短い)、Phase 2/3 で範囲拡大時は要注意。

★ Adversary agent 指摘 fix: Context prefix の prompt 注入剥がし
- chunk doc に baked-in した `[Context: ...]` が retrieval → prompt 連結時に
  bot 応答に literal 漏出する経路を遮断するため、本 module で `strip_context_prefix()`
  helper 提供。brain_index.search() で `metadata.has_context=True` の chunk は
  出口で自動 strip される。
"""
from __future__ import annotations

import logging
from typing import Optional

import httpx

logger = logging.getLogger("contextual_retrieval")


# ─── Contextual Retrieval prompt (Anthropic cookbook 由来) ────────────────
# document block と chunk block で role を分ける、cache_control は document block のみに付与。
_CHUNK_PROMPT = """Here is the chunk we want to situate within the whole document:
<chunk>
{chunk}
</chunk>

Please give a short succinct context to situate this chunk within the overall document for the purposes of improving search retrieval of the chunk. Answer only with the succinct context and nothing else."""


# 公式 cookbook の閾値 (= Haiku 4.5 の min cache token 4096 ≈ 約 8000 char 日本語)
# 短い document は cache 効かない & そもそも context generate コスト > 効果 なので skip
MIN_DOCUMENT_CHARS_FOR_CONTEXT = 4000

# 失敗時 fallback marker (= dataset audit / debug 用)
NO_CONTEXT_PREFIX = ""  # 空 (= 元 chunk のまま)


# ─── Context prefix strip (★2026-05-23 Adversary agent 指摘の致命 bug fix) ────
# Contextual Retrieval は **prefix を embedding 側のみで使用、prompt 注入時は剥がす** が公式 pattern。
# 私 (主 agent) は brain_index.py で chunk doc に baked-in したため、retrieval → prompt 連結地点で
# `[Context: ...]` が literal で bot 応答に漏出する経路があった。本 helper で 1 行剥がし。
import re as _re

_CONTEXT_PREFIX_RE = _re.compile(r"^\[Context:[^\]]+\]\s*\n+", _re.MULTILINE)


def strip_context_prefix(chunk: str) -> str:
    """retrieval 用に prepend した `[Context: ...]` prefix を剥がす。

    brain_index.index_wiki_file() で contextualize_chunks() 結果を chromadb に upsert。
    chunk doc に `[Context: ...]` が baked-in されてる状態。bot 応答 prompt に注入する時に
    本 helper を必ず通すこと (= 公式 cookbook の prompt 注入剥がし invariant)。

    Args:
        chunk: chromadb から取得した chunk doc (= contextual prefix 含む可能性)
    Returns:
        prefix を剥がした本文 (= 元の wiki 内容)
    """
    return _CONTEXT_PREFIX_RE.sub("", chunk, count=1)


async def contextualize_chunks(
    document_text: str,
    chunks: list[str],
    http: httpx.AsyncClient,
    litellm_url: str,
    litellm_key: str,
    model: str = "contextualize",  # haiku 4.5 (= litellm_config の model_name)
    timeout: float = 30.0,
) -> tuple[list[str], dict]:
    """各 chunk に context prefix を生成し、prepend した chunk list を返す。

    Args:
        document_text: 元 document 全文 (= wiki file content)、cache 対象
        chunks: split 済 chunks
        http: httpx async client
        litellm_url: LiteLLM proxy URL
        litellm_key: LiteLLM master key
        model: litellm の model_name (default "contextualize" = Haiku 4.5)
        timeout: 1 chunk あたりの timeout (s)

    Returns:
        (contextualized_chunks, stats)
        - contextualized_chunks: prepend 済 chunk list
          (= 「[Context: <50-100 token>]\\n\\n<original chunk>」)
        - stats: {"n_succeed": int, "n_failed": int, "skipped_short_doc": bool}
    """
    stats = {
        "n_succeed": 0,
        "n_failed": 0,
        "skipped_short_doc": False,
        "model": model,
    }

    # document が短すぎる → context 生成 skip (= cache 効果 0 + cost に見合わない)
    if len(document_text) < MIN_DOCUMENT_CHARS_FOR_CONTEXT:
        stats["skipped_short_doc"] = True
        logger.debug(
            f"contextualize skip: document {len(document_text)} chars < "
            f"{MIN_DOCUMENT_CHARS_FOR_CONTEXT} threshold"
        )
        return chunks, stats

    out: list[str] = []
    for chunk in chunks:
        try:
            resp = await http.post(
                f"{litellm_url}/v1/chat/completions",
                headers={"Authorization": f"Bearer {litellm_key}"},
                json={
                    "model": model,
                    "messages": [
                        {
                            "role": "user",
                            "content": [
                                # block 1: document 全文 + cache 指示 (1h TTL)
                                {
                                    "type": "text",
                                    "text": f"<document>\n{document_text}\n</document>",
                                    "cache_control": {
                                        "type": "ephemeral",
                                        "ttl": "1h",
                                    },
                                },
                                # block 2: chunk と質問 (dynamic、cache 対象外)
                                {
                                    "type": "text",
                                    "text": _CHUNK_PROMPT.format(chunk=chunk),
                                },
                            ],
                        }
                    ],
                    "max_tokens": 150,
                    "temperature": 0.0,  # 一貫性確保
                },
                timeout=timeout,
            )
            resp.raise_for_status()
            data = resp.json()
            context = (data["choices"][0]["message"]["content"] or "").strip()
            if not context:
                raise ValueError("empty context returned")
            out.append(f"[Context: {context}]\n\n{chunk}")
            stats["n_succeed"] += 1
        except Exception as e:
            # 失敗 chunk は context 無しで元 chunk のまま (= retrieval 動作維持)
            logger.warning(f"contextualize chunk failed: {type(e).__name__}: {str(e)[:200]}")
            out.append(chunk)
            stats["n_failed"] += 1

    return out, stats


def estimate_cost(
    n_chunks: int,
    avg_doc_chars: int = 4000,
    cached: bool = True,
) -> dict:
    """contextualize の概算 cost (USD) を返す (= 海山 / 私の事前判断用)。

    Haiku 4.5 pricing (2026-05 時点):
    - input (uncached): $1.00 / MTok
    - input (cache read): $0.10 / MTok (= 90% off)
    - output: $5.00 / MTok
    """
    avg_doc_tokens = avg_doc_chars // 4  # 日本語 1 token ≈ 4 char ざっくり
    chunk_tokens_avg = 200  # chunk + prompt = ~200 token
    output_tokens = 100  # context output ~100 token

    if cached:
        # cache miss 1 回 + cache hit (N-1) 回
        input_cost = (avg_doc_tokens + chunk_tokens_avg) * 1.0 / 1_000_000  # 1 回目 full
        input_cost += (n_chunks - 1) * (avg_doc_tokens * 0.1 + chunk_tokens_avg * 1.0) / 1_000_000
    else:
        input_cost = n_chunks * (avg_doc_tokens + chunk_tokens_avg) * 1.0 / 1_000_000

    output_cost = n_chunks * output_tokens * 5.0 / 1_000_000
    return {
        "input_usd": round(input_cost, 4),
        "output_usd": round(output_cost, 4),
        "total_usd": round(input_cost + output_cost, 4),
        "n_chunks": n_chunks,
        "cached": cached,
    }
