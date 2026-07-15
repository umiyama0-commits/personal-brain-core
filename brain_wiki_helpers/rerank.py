"""Cohere Rerank 3.5 統合 (★2026-05-24 Plan C v2 Step 2、海山「進めて」).

# 役割

chromadb embedding 検索で広く拾った top-K candidate を Cohere Rerank で
「実際の query 関連性」順に並べ替え、上位 top-N を context に注入。

# なぜ必要か (= Fact-checker + Strategy reviewer 共通指摘)

embedding 距離 ≠ 「query にとっての真の関連性」
- embedding は意味の近さ、ただし「客単価教えて」に対し「客単価の話題」chunk より
  「業績全般」chunk が embedding 上 近く出ることがある
- 47-66% query で generator が top-rank doc 無視という研究結果あり
- Cohere Rerank 3.5 = cross-encoder、query + doc pair で正確に並び順を計算

# spec (= verification subagent 2026-05-24 確認後の確定値)

- model: **`rerank-v3.5`** (= 英語 / 多言語統合、日本語 native、Cohere 2024-12 リリース)
  - ★旧名「rerank-multilingual-v3.5」は **存在しない**、v3.5 は単一統合モデル
  - 将来 upgrade: Rerank 4 Fast / Pro (= token 課金、大量 doc 時に有利)
- endpoint: `POST https://api.cohere.com/v2/rerank` (= v1 併存だが v2 推奨)
- authentication: `Authorization: Bearer <COHERE_API_KEY>` (= Bearer 大文字)
- pricing: $2.00 / 1,000 searches (= 1 search = 1 query + 最大 100 docs)
  - 500 token 超 doc は自動 chunk 分割、各 chunk が 1 doc としてカウント
- max documents per query: 1000 (= chromadb top 30 なら余裕)
- rate limit: Trial 10 req/min (= 月 1,000 calls)、Production 1,000 req/min
- 日本語サポート: SOTA 10 言語に日本語明示 (= Arabic/Chinese/French/German/Hindi/
  Japanese/Korean/Portuguese/Russian/Spanish)、Context length 4096
- latency: +150-300ms (= LINE Bot 全体 ~3-5s の中で誤差レベル)
- return_documents: false 推奨 (= response 軽量化、{index, relevance_score} のみ返却)

# graceful degradation

- COHERE_API_KEY 未設定 → skip (= 旧 retrieval flow 維持)
- API 接続失敗 → 元 hits 順序維持 (= None 返却で caller fallback)
- timeout 10s 超 → 同上
- = retrieval pipeline は壊れない、Cohere は「精度向上のための加速器」

# cost 想定

LINE Bot 質問 50-200 件/日 × 30 日 = 1,500-6,000 件/月 = **$3-12/月**
"""
from __future__ import annotations

import logging
import os
from typing import Optional

import httpx

logger = logging.getLogger("rerank")

# ★2026-06-08 海山指示「各種 API 残高枯渇の自動連絡」: Cohere の枯渇 (402 等) を error 経路で検知。
try:
    import sys as _sys
    from pathlib import Path as _Path
    _scripts = str(_Path(__file__).resolve().parent.parent / "scripts")
    if _scripts not in _sys.path:
        _sys.path.insert(0, _scripts)
    from quota_alert import maybe_alert_quota as _maybe_alert_quota  # type: ignore
except Exception:  # pragma: no cover
    def _maybe_alert_quota(*_a, **_k):
        return False

COHERE_API_URL = "https://api.cohere.com/v2/rerank"
# ★2026-05-24 fact-checker verify: rerank-v3.5 (= 英語/多言語統合、旧 multilingual-v3.5 は存在しない)
# 将来 Rerank 4 Fast upgrade も検討 (= COHERE_RERANK_MODEL env 上書きで切替可)
COHERE_RERANK_MODEL = os.getenv("COHERE_RERANK_MODEL", "rerank-v3.5")
COHERE_API_KEY_ENV = "COHERE_API_KEY"

# 上限 (= API spec 由来 + 安全側)
MAX_DOCUMENTS_PER_QUERY = 50    # Cohere 上限 1000、ただし latency / cost で 50 で十分
MAX_DOCUMENT_CHARS = 4000        # 1 doc の最大文字数 (= Cohere は token 単位、4000 char ≈ 1000 token 推定)
MAX_QUERY_CHARS = 1000
DEFAULT_TIMEOUT = 10.0


async def cohere_rerank(
    query: str,
    documents: list[str],
    http: httpx.AsyncClient,
    top_n: int = 10,
    model: str = COHERE_RERANK_MODEL,
    timeout: float = DEFAULT_TIMEOUT,
) -> Optional[list[dict]]:
    """Cohere Rerank API で documents を query 関連性順に並べ替え。

    Args:
        query: 検索 query
        documents: chromadb 取得 chunks (= top-K、N>=top_n が必要)
        http: httpx.AsyncClient (= 既存 brain_wiki.py の self.http 流用)
        top_n: 戻す件数 (default 10)
        model: rerank model (default rerank-v3.5)
        timeout: API timeout (s)

    Returns:
        success: [{"index": int, "relevance_score": float}, ...] (= top_n 件、relevance 高い順)
        failure: None (= caller は documents の順序維持で続行)
    """
    api_key = os.getenv(COHERE_API_KEY_ENV, "").strip()
    if not api_key:
        logger.debug("COHERE_API_KEY not set, skip rerank (graceful degradation)")
        return None

    if not query or not documents:
        return None

    # 入力 truncate (= API 仕様 + cost 安全側)
    safe_documents = [str(d)[:MAX_DOCUMENT_CHARS] for d in documents[:MAX_DOCUMENTS_PER_QUERY]]
    safe_query = str(query)[:MAX_QUERY_CHARS]
    safe_top_n = min(top_n, len(safe_documents))

    try:
        resp = await http.post(
            COHERE_API_URL,
            headers={
                # ★2026-05-24 fact-checker verify: Bearer (大文字) 推奨
                "Authorization": f"Bearer {api_key}",
                "Content-Type": "application/json",
            },
            json={
                "model": model,
                "query": safe_query,
                "documents": safe_documents,
                "top_n": safe_top_n,
                # ★2026-05-24: response 軽量化 (= {index, relevance_score} のみ、document 全文不要)
                "return_documents": False,
                # ★4096 token = Cohere default、明示で stable
                "max_tokens_per_doc": 4096,
            },
            timeout=timeout,
        )
        resp.raise_for_status()
        data = resp.json()
        results = data.get("results", [])
        # 結果 sanity check
        if not isinstance(results, list):
            logger.warning(f"cohere_rerank unexpected results type: {type(results)}")
            return None
        return results
    except httpx.HTTPStatusError as e:
        # 401 (= invalid key) / 402 (= 残高切れ) / 429 (= rate limit) / 500 等
        logger.warning(f"cohere_rerank HTTP error: {e.response.status_code} {e.response.text[:200]}")
        _log_failure("HTTPStatusError", f"{e.response.status_code}: {e.response.text[:120]}")
        # ★枯渇 (402/insufficient 等) なら LINE 通知 (rate-limit は classify が無視)
        try:
            _maybe_alert_quota("cohere(rerank)", status_code=e.response.status_code,
                               body_text=(e.response.text or "")[:1000])
        except Exception:
            pass
        return None
    except httpx.TimeoutException:
        logger.warning(f"cohere_rerank timeout (>{timeout}s)")
        _log_failure("TimeoutException", f"timeout >{timeout}s")
        return None
    except Exception as e:
        logger.warning(f"cohere_rerank error: {type(e).__name__}: {str(e)[:200]}")
        _log_failure(type(e).__name__, str(e)[:200])
        return None


def _log_failure(error_class: str, error_msg: str) -> None:
    """★2026-05-24 Tier 1: bot_uptime_monitor が component_streak で拾えるよう
    failure を turn_failed event として記録 (= graceful degradation 維持しつつ可視化)."""
    try:
        from scripts.bot_events import log_bot_event  # type: ignore
        log_bot_event(
            "cohere_rerank", "turn_failed",
            error_class=error_class,
            error_msg=error_msg,
        )
    except Exception:
        # bot_events 自体が壊れててもこの関数で bot を止めない
        pass


def estimate_cost(n_queries_per_month: int) -> dict:
    """月次 cost 概算 (= Cohere pricing $2.00 / 1000 searches base)。"""
    usd_per_month = round(n_queries_per_month * 0.002, 2)
    return {
        "n_queries_per_month": n_queries_per_month,
        "usd_per_month": usd_per_month,
        "usd_per_query": 0.002,
        "model": COHERE_RERANK_MODEL,
    }
