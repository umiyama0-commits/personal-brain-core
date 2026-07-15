"""_common.py の pure function を中心にテスト。

LLM 呼び出しは httpx 層を mock してリトライ挙動を検証する。
"""
from __future__ import annotations

import asyncio
import json

import httpx
import pytest


# ─── frontmatter ─────────────────────────────────
def test_parse_frontmatter_basic(common):
    content = (
        "---\n"
        "type: style_pattern\n"
        "id: style-001\n"
        "evidence: [a.md, b.md]\n"
        "confidence: medium\n"
        "---\n"
        "# body here\n"
    )
    fm, body = common.parse_frontmatter(content)
    assert fm["type"] == "style_pattern"
    assert fm["id"] == "style-001"
    assert fm["evidence"] == ["a.md", "b.md"]
    assert fm["confidence"] == "medium"
    assert body.startswith("# body here")


def test_parse_frontmatter_no_frontmatter(common):
    fm, body = common.parse_frontmatter("# just a heading\n")
    assert fm == {}
    assert body == "# just a heading\n"


def test_render_frontmatter_roundtrip(common):
    fm = {
        "type": "judgment_pattern",
        "id": "judgment-x-001",
        "evidence": ["raw/a.md", "wiki/b.md"],
        "confidence": "high",
    }
    text = common.render_frontmatter(fm)
    parsed, _ = common.parse_frontmatter(text + "body\n")
    assert parsed["type"] == "judgment_pattern"
    assert parsed["evidence"] == ["raw/a.md", "wiki/b.md"]


# ─── safe_id / next_index ─────────────────────────
def test_safe_id_strips_unsafe_chars(common):
    assert common.safe_id("style", "Hello World!", 1) == "style-hello-world-001"
    assert common.safe_id("judgment", "強気な判断", 5) == "judgment-x-005"  # 全角 → 全削除 → "x"
    assert common.safe_id("reflex", "test", 0) == "reflex-test"


def test_next_index_empty_dir(common, brain_root):
    layer_dir = brain_root / "data" / "brain" / "wiki" / "style"
    assert common.next_index(layer_dir, "style") == 1


def test_next_index_with_existing(common, brain_root):
    layer_dir = brain_root / "data" / "brain" / "wiki" / "style"
    (layer_dir / "style-foo-001.md").write_text("---\n---\n", encoding="utf-8")
    (layer_dir / "style-foo-003.md").write_text("---\n---\n", encoding="utf-8")
    (layer_dir / "style-bar-002.md").write_text("---\n---\n", encoding="utf-8")
    assert common.next_index(layer_dir, "style") == 4


# ─── ExtractorState ───────────────────────────────
def test_extractor_state_roundtrip(common, brain_root):
    s = common.ExtractorState.load("test_layer")
    assert s.processed_files == {}
    s.processed_files["a.md"] = "abc123"
    s.counters["written"] = 5
    s.save()

    s2 = common.ExtractorState.load("test_layer")
    assert s2.processed_files == {"a.md": "abc123"}
    assert s2.counters == {"written": 5}
    assert s2.last_run  # ISO timestamp recorded


# ─── extract_json_block ───────────────────────────
def test_extract_json_block_plain(common):
    s = '[{"a": 1}, {"b": 2}]'
    assert common.extract_json_block(s) == [{"a": 1}, {"b": 2}]


def test_extract_json_block_fenced(common):
    s = "```json\n[{\"a\": 1}]\n```"
    assert common.extract_json_block(s) == [{"a": 1}]


def test_extract_json_block_unfenced_code(common):
    s = "```\n{\"a\": 1}\n```"
    assert common.extract_json_block(s) == {"a": 1}


# ─── parse_llm_json_array (新規) ───────────────────
def test_parse_llm_json_array_valid(common):
    text = '[{"category": "x", "context": "y", "pattern": "p"}]'
    out = common.parse_llm_json_array(text, required_keys=("category", "context", "pattern"))
    assert len(out) == 1
    assert out[0]["category"] == "x"


def test_parse_llm_json_array_skip_missing_keys(common):
    text = '[{"category": "x"}, {"category": "y", "context": "z", "pattern": "p"}]'
    out = common.parse_llm_json_array(text, required_keys=("category", "context", "pattern"))
    assert len(out) == 1
    assert out[0]["category"] == "y"


def test_parse_llm_json_array_skip_non_dict(common):
    text = '["string", {"a": 1, "b": 2}]'
    out = common.parse_llm_json_array(text, required_keys=("a", "b"))
    assert len(out) == 1


def test_parse_llm_json_array_not_list_raises(common):
    with pytest.raises(common.LLMContractError):
        common.parse_llm_json_array('{"not": "list"}')


def test_parse_llm_json_array_invalid_json_raises(common):
    with pytest.raises(common.LLMContractError):
        common.parse_llm_json_array("not a json")


def test_parse_llm_json_array_empty_is_valid(common):
    """空配列は LLM が "本当に 0 件" と判断したケースで正常。"""
    out = common.parse_llm_json_array("[]", required_keys=("a",))
    assert out == []


# ─── log_event / run_context ──────────────────────
def test_log_event_writes_jsonl(common):
    common.log_event("test_extractor", "test_event", foo="bar", n=42)
    assert common.EVENTS_LOG.exists()
    lines = common.EVENTS_LOG.read_text(encoding="utf-8").strip().splitlines()
    assert len(lines) >= 1
    rec = json.loads(lines[-1])
    assert rec["extractor"] == "test_extractor"
    assert rec["event"] == "test_event"
    assert rec["foo"] == "bar"
    assert rec["n"] == 42
    assert "ts" in rec


def test_run_context_records_start_finish(common):
    with common.run_context("ctx_test", source="x") as ctx:
        ctx["items_written"] = 7

    lines = common.EVENTS_LOG.read_text(encoding="utf-8").strip().splitlines()
    events = [json.loads(l) for l in lines if "ctx_test" in l]
    assert any(e["event"] == "run_started" for e in events)
    finished = [e for e in events if e["event"] == "run_finished"]
    assert len(finished) >= 1
    f = finished[-1]
    assert f["source"] == "x"
    assert f["items_written"] == 7
    assert "elapsed_sec" in f


def test_run_context_records_failure(common):
    with pytest.raises(ValueError):
        with common.run_context("ctx_fail_test"):
            raise ValueError("boom")

    lines = common.EVENTS_LOG.read_text(encoding="utf-8").strip().splitlines()
    events = [json.loads(l) for l in lines if "ctx_fail_test" in l]
    failed = [e for e in events if e["event"] == "run_failed"]
    assert len(failed) >= 1
    assert failed[-1]["error_class"] == "ValueError"
    assert "boom" in failed[-1]["error_msg"]


# ─── call_llm_with_retry (httpx mock) ──────────────
class _MockTransport(httpx.AsyncBaseTransport):
    """N 回失敗 → その後成功する transport。"""

    def __init__(self, fail_n: int, status_code_on_fail: int = 503):
        self.fail_n = fail_n
        self.status_code_on_fail = status_code_on_fail
        self.calls = 0

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        self.calls += 1
        if self.calls <= self.fail_n:
            return httpx.Response(self.status_code_on_fail, json={"error": "upstream"})
        return httpx.Response(
            200,
            json={"choices": [{"message": {"content": "OK response"}}]},
        )


def test_call_llm_with_retry_succeeds_after_failures(common):
    """503 を 2 回返したあと成功するケース → リトライで通る"""
    transport = _MockTransport(fail_n=2, status_code_on_fail=503)

    async def run():
        async with httpx.AsyncClient(transport=transport, base_url="http://litellm:4000") as http:
            return await common.call_llm_with_retry(
                http,
                prompt="hello",
                retries=4,
                backoff_base=1.01,  # ほぼ即時リトライ
                extractor_name="test",
            )

    result = asyncio.run(run())
    assert result == "OK response"
    assert transport.calls == 3


def test_call_llm_with_retry_gives_up(common):
    """全部失敗するケース → LLMContractError"""
    transport = _MockTransport(fail_n=99, status_code_on_fail=502)

    async def run():
        async with httpx.AsyncClient(transport=transport, base_url="http://litellm:4000") as http:
            return await common.call_llm_with_retry(
                http,
                prompt="hello",
                retries=2,
                backoff_base=1.01,
                extractor_name="test",
            )

    with pytest.raises(common.LLMContractError):
        asyncio.run(run())
    assert transport.calls == 2  # retries 回試行
