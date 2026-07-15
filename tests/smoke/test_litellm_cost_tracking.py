"""smoke test: LiteLLM コスト tracking v2 (★2026-05-27 海山指示、safer 再実装)

v1 (= 7749600) は 「お休みをいただいてます」 fallback 連発で 即 revert.
真因 (= Subagent fact-check 確定): LiteLLM response の脆い indexing
  (a) error response `{"error":{...}}` → KeyError: 'choices'
  (b) choices=[] → IndexError
  (c) content=None (= moderation / tool_use only) → TypeError
  (d) content が list (= /v1/messages 経路) → str 期待違反

v2 設計:
1. defensive parsing (_safely_extract_content) を llm_retry に install
   → 旧 code の潜在 bug も同時 fix
2. env flag COST_TRACKING_ENABLED (default OFF) で gradual rollout
   → 海山 verify 後に ON 切替、issue 出ても env で即 rollback
3. return_full=True で (response_data dict, content str) tuple return
   → caller が usage / actual model 取れる
"""
from __future__ import annotations

from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent.parent


# ─── L1: defensive parsing (= 旧 code 潜在 bug 解消、v3 で raise しない) ─────
@pytest.mark.smoke
def test_safely_extract_content_function_exists():
    """_safely_extract_content helper が存在 + 4 edge case ガード."""
    src = (REPO / "brain_wiki_helpers" / "llm_retry.py").read_text(encoding="utf-8")
    assert "def _safely_extract_content" in src
    # case (a) error response
    assert '"error"' in src and '"choices" not in response_data' in src
    # case (b) choices 空
    assert 'choices = response_data.get("choices")' in src
    # case (c) content=None → 空文字
    assert 'content or ""' in src
    # case (d) content が list → text concat
    assert "isinstance(content, list)" in src


@pytest.mark.smoke
def test_safely_extract_content_does_not_raise_returns_empty():
    """★v3 (2026-05-27 海山指示): defensive parsing は raise しない、空文字 return + warning log.

    v2 では RuntimeError raise → 「お休みをいただいてます」 fallback 経路に行ってた.
    v3 では empty return → caller の empty guard 経路 (= 「うまく言葉が出なかった」) に統一.
    """
    src = (REPO / "brain_wiki_helpers" / "llm_retry.py").read_text(encoding="utf-8")
    fn_idx = src.find("def _safely_extract_content")
    end_idx = src.find("\nasync def post_litellm_with_retry", fn_idx)
    assert fn_idx > 0 and end_idx > fn_idx
    body = src[fn_idx:end_idx]
    # raise RuntimeError が 関数 body に無いこと (= v2 から v3 への変更を確認)
    assert "raise RuntimeError" not in body, (
        "v3 では raise しない設計、空文字 return + warning log にする"
    )
    # 各 edge case で logger.warning 経由
    assert body.count("logger.warning") >= 3
    # 各 edge case で return ""
    assert body.count('return ""') >= 3


@pytest.mark.smoke
def test_post_litellm_with_retry_uses_defensive_parsing():
    """post_litellm_with_retry が _safely_extract_content 経由で content 取得."""
    src = (REPO / "brain_wiki_helpers" / "llm_retry.py").read_text(encoding="utf-8")
    # 旧 直接 indexing は使わない (= 脆い)
    assert 'return resp.json()["choices"][0]["message"]["content"]' not in src
    # 新 defensive 関数経由
    assert "_safely_extract_content(response_data)" in src


@pytest.mark.smoke
def test_return_full_returns_tuple_dict_and_str():
    """return_full=True で (dict, str) tuple 返却."""
    src = (REPO / "brain_wiki_helpers" / "llm_retry.py").read_text(encoding="utf-8")
    assert "return response_data, content" in src
    # default は str のみ
    assert "if return_full:" in src


# ─── L2: env flag gate (= safer rollout、v1 事故再発防止) ─────
@pytest.mark.smoke
def test_cost_tracking_gated_by_env_flag_default_off():
    """COST_TRACKING_ENABLED env flag で gate、default OFF (= v1 事故再発防止)."""
    src = (REPO / "brain_wiki.py").read_text(encoding="utf-8")
    assert 'os.getenv("COST_TRACKING_ENABLED"' in src
    # default "0" (= OFF) で 旧挙動維持
    assert 'getenv("COST_TRACKING_ENABLED", "0")' in src


@pytest.mark.smoke
def test_call_llm_branches_on_flag():
    """_call_llm が flag ON 時のみ return_full=True path、OFF で旧 str return path."""
    src = (REPO / "brain_wiki.py").read_text(encoding="utf-8")
    idx = src.find("async def _call_llm() -> str:")
    assert idx > 0
    body = src[idx : idx + 2000]
    # flag ON 時の path
    assert "if _cost_tracking_enabled:" in body
    assert "return_full=True" in body
    assert "_captured_response.clear()" in body
    # flag OFF 時の path (= 旧挙動)
    assert "旧 path" in body or "return await self._post_litellm_with_retry(" in body


@pytest.mark.smoke
def test_ctx_usage_only_when_flag_on():
    """flag ON + _captured_response あり の時のみ ctx に usage / model 設定."""
    src = (REPO / "brain_wiki.py").read_text(encoding="utf-8")
    ctx_idx = src.find('ctx["status"] = "ok"')
    assert ctx_idx > 0
    window = src[ctx_idx : ctx_idx + 1500]
    # 条件分岐
    assert "if _cost_tracking_enabled and _captured_response:" in window
    # usage / actual model
    assert 'ctx["usage"] = _usage' in window
    assert 'ctx["model"] = _actual_model' in window


# ─── L3: 後方互換 (= 既存 caller 全部 影響なし) ─────
@pytest.mark.smoke
def test_existing_callers_unaffected():
    """既存 caller (= clone_memory_update / 他) は return_full 指定なし → 旧 str return path."""
    src = (REPO / "brain_wiki.py").read_text(encoding="utf-8")
    # 実関数呼出で return_full=True を渡してる箇所は _call_llm 内のみ (= 末尾カンマ付き)
    # docstring / コメント内の言及は除外
    import re
    # `return_full=True,` (= 引数として渡してる、行末又はカンマ続き) のみ count
    call_pattern = re.compile(r"^\s*return_full=True,\s*$", re.MULTILINE)
    matches = call_pattern.findall(src)
    assert len(matches) == 1, (
        f"return_full=True 関数 invocation 想定外 ({len(matches)} 件)、"
        "_call_llm 内 1 箇所のみ予期"
    )
