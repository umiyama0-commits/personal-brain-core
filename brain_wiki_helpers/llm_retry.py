"""
brain_wiki_helpers/llm_retry.py — LiteLLM 呼び出しの retry mechanism (★2026-05-22 Phase 3a)

brain_wiki.BrainWiki._post_litellm_with_retry を pure async function に切り出し。
self.http / self.litellm_url / self.litellm_key を引数化して self 依存を解消。

★2026-05-22 動機: alignment_trial run5/6 で 429 連発 + 社員 DM memory_update 100% fail を
受けて、構造的予防として実装した retry。仕様詳細は docstring 参照。
"""
from __future__ import annotations

import asyncio
import logging
import random
from typing import Any

import httpx

logger = logging.getLogger(__name__)

# ★2026-06-08 海山指示「各種 API 残高枯渇の自動連絡」: LLM error 経路で枯渇を検知して LINE 通知。
# soft import (= 失敗しても no-op、hot path を絶対壊さない)。成功 path では一切呼ばない。
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


def _safely_extract_content(response_data: Any) -> str:
    """LiteLLM response から content を defensive に取り出す.

    ★2026-05-27 海山指示 (= 「お休みをいただいてます」 連発の真因 fix v3):
    v2 では RuntimeError raise → caller try/except 経由で「お休みをいただいてます」 fallback
    発火してた (= 海山 confirm 後も依然出てた). v3 は raise せず **空文字 return + warning log**
    に変更. 結果として caller の empty guard 「うまく言葉が出なかった」 が user に届く
    (= まだ fallback だが、root cause が log で明確、ユーザ印象も若干 改善).

    旧 code `response_data["choices"][0]["message"]["content"]` で raise する 4 case:
      (a) error response `{"error":{...}}` → choices 無
      (b) choices=[] (= moderation / 上流 error pass-through)
      (c) content=None (= tool_use only / thinking-only response、Opus 4.7 で実例あり)
      (d) content が list of blocks (= /v1/messages 経路 / thinking 混在)
    Subagent fact-check 済 (LiteLLM issue #27946 / #25877 / #24176).

    本関数で 4 case 全て **空文字 return** (= warning log で trace 可)、caller の
    empty guard と協調.
    """
    if not isinstance(response_data, dict):
        logger.warning(
            f"litellm response not dict: {type(response_data).__name__}: "
            f"{str(response_data)[:200]}"
        )
        return ""
    if "error" in response_data and "choices" not in response_data:
        err = response_data.get("error")
        logger.warning(f"litellm error response: {str(err)[:300]}")
        return ""
    choices = response_data.get("choices") or []
    if not choices:
        logger.warning(
            f"litellm choices empty (moderation / upstream error?): "
            f"{str(response_data)[:200]}"
        )
        return ""
    msg = choices[0].get("message") if isinstance(choices[0], dict) else {}
    if not isinstance(msg, dict):
        msg = {}
    content = msg.get("content")
    # content が list of blocks (= /v1/messages 経路 / thinking 混在) → text を concat
    if isinstance(content, list):
        content = "".join(
            b.get("text", "")
            for b in content
            if isinstance(b, dict) and b.get("type") == "text"
        )
    # content=None / "" は空文字 として return (= caller 側で empty guard が引き継ぐ)
    return content or ""


async def post_litellm_with_retry(
    http: httpx.AsyncClient,
    litellm_url: str,
    litellm_key: str,
    payload: dict,
    *,
    timeout: float = 60.0,
    max_retries: int = 3,
    backoff_base: float = 1.5,
    return_full: bool = False,
):
    """LiteLLM /v1/chat/completions に POST、429/5xx/network error で retry。

    retry policy:
    - max_retries=3 (= 計 3 回試行)
    - backoff: 1.5^attempt + jitter (= ~1.5s / 2.25s / 3.4s)
    - retry 対象: HTTP 429 / 5xx / httpx.TimeoutException / httpx.NetworkError
    - retry しない: 4xx (= 401, 403, 404 等の認証 / 設定エラー)、JSON parse 失敗

    payload は LiteLLM が受ける完全な dict (= {model, messages, max_tokens, ...})。
    最終失敗時は例外を re-raise。

    ★2026-05-27 海山指示: defensive content parsing (= _safely_extract_content) を
    install、error response / content=None / list 等の edge case を guard.

    Args:
        http: 共有 httpx.AsyncClient (= caller が close 管理)
        litellm_url: LiteLLM proxy のベース URL (例: http://litellm:4000)
        litellm_key: LiteLLM master key
        payload: /v1/chat/completions に投げる完全な dict
        timeout: 1 試行あたりの timeout (秒)
        max_retries: 最大試行回数
        backoff_base: backoff の指数 base (1.5 推奨)
        return_full: True で `(response_data: dict, content: str)` tuple return.
                     False (default) なら 旧挙動 = content: str のみ return.
                     ★2026-05-27 海山指示 LiteLLM 日次コスト tracking: caller が
                     usage 取れるように tuple 経由で提供.
    """
    last_err: Exception | None = None
    for attempt in range(1, max_retries + 1):
        try:
            resp = await http.post(
                f"{litellm_url}/v1/chat/completions",
                headers={"Authorization": f"Bearer {litellm_key}"},
                json=payload,
                timeout=timeout,
            )
            # 429 / 5xx は retry
            if resp.status_code == 429 or resp.status_code >= 500:
                last_err = httpx.HTTPStatusError(
                    f"upstream {resp.status_code}",
                    request=resp.request,
                    response=resp,
                )
                # ★枯渇シグナル (insufficient_quota/billing 等) なら LINE 通知 (cooldown 付・非ブロッキング)。
                # 単なる rate-limit 429 は classify が None を返し alert しない。
                try:
                    _maybe_alert_quota("llm(litellm)", status_code=resp.status_code,
                                       body_text=(resp.text or "")[:1000])
                except Exception:
                    pass
                logger.warning(
                    f"post_litellm_with_retry attempt {attempt}/{max_retries}: "
                    f"status {resp.status_code} (model={payload.get('model')})"
                )
                if attempt < max_retries:
                    sleep_for = backoff_base ** attempt + random.uniform(0, 0.5)
                    await asyncio.sleep(sleep_for)
                    continue
                # 最終 attempt で 429/5xx なら例外
                resp.raise_for_status()
            # 2xx 系は成功
            resp.raise_for_status()
            response_data = resp.json()
            # defensive parsing (= edge case 4 種 guard、★2026-05-27 海山指示)
            content = _safely_extract_content(response_data)
            if return_full:
                return response_data, content
            return content
        except httpx.HTTPStatusError as e:
            # 429/5xx 以外の HTTPStatusError (= 4xx other) は即 raise
            status = e.response.status_code if e.response is not None else 0
            # ★402 Payment Required 等の 4xx 枯渇シグナルを raise 前に検知 (classify が非枯渇は無視)。
            try:
                _maybe_alert_quota(
                    "llm(litellm)", status_code=status,
                    body_text=((e.response.text if e.response is not None else "") or "")[:1000],
                )
            except Exception:
                pass
            if status != 0 and status != 429 and status < 500:
                raise
            last_err = e
            if attempt < max_retries:
                sleep_for = backoff_base ** attempt + random.uniform(0, 0.5)
                await asyncio.sleep(sleep_for)
                continue
            raise
        except (httpx.TimeoutException, httpx.NetworkError) as e:
            last_err = e
            logger.warning(
                f"post_litellm_with_retry attempt {attempt}/{max_retries}: "
                f"{type(e).__name__}: {e} (model={payload.get('model')})"
            )
            if attempt < max_retries:
                sleep_for = backoff_base ** attempt + random.uniform(0, 0.5)
                await asyncio.sleep(sleep_for)
                continue
            raise
    # 通常ここまで来ない (= 上の raise で抜ける)
    if last_err:
        raise last_err
    return ""
