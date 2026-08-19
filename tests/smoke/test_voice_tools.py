"""tests/smoke/test_voice_tools.py — 通話中 PB retrieval (brain_search) の契約 pin
(★2026-07-12 音声フェーズ Phase 1、§1.15(b) cross-check 3 体反映).

守る不変条件:
- tool 定義に server/secret を**含まない** (Fact-check: server.secret は legacy で届く保証
  無し + web-config がブラウザへ config を返すため secret を埋めると漏洩 = F1 BLOCKER)
- Vapi 公式 spec に一致 (tool-calls 両形対応 / results に toolCallId+name = spec required)
- tool は trusted config のみ (untrusted 縮退 config には付かない = source pin)
- 検索整形は音声安全 (数字の桁途中で切らない / markdown ノイズ除去 / source dedup)
- 防御的 (str/dict arguments 揺れ・未知 tool・空 query・index 未起動・検索例外)
"""
from __future__ import annotations

import asyncio
import json
from pathlib import Path

from services import voice_tools as vt

_ROOT = Path(__file__).resolve().parents[2]


def _run(coro):
    return asyncio.run(coro)


# ── tool definition (Vapi spec 形 + secret 非包含 pin) ──────
def test_tool_definition_shape_and_no_server_block():
    d = vt.tool_definition()
    assert d["type"] == "function"
    assert d["function"]["name"] == "brain_search"
    assert d["function"]["parameters"]["required"] == ["query"]
    # ★F1 BLOCKER 再発防止: server ブロック (= secret の置き場) を持たないこと
    assert "server" not in d
    # 実行中 filler (無音対策)
    types = [m.get("type") for m in d.get("messages", [])]
    assert "request-start" in types


def test_tool_never_embeds_env_secrets(monkeypatch):
    """web-config はこの config をそのままブラウザへ返す — 電話用/秘匿 secret が
    tool 経由で混入しないことを env sentinel で pin (Reviewer F6)。"""
    monkeypatch.setenv("VAPI_SECRET", "PHONE_LEAK_SENTINEL")
    monkeypatch.setenv("VAPI_WEB_SECRET", "WEB_LEAK_SENTINEL")
    blob = json.dumps(vt.attach_brain_search(_cfg()), ensure_ascii=False)
    assert "PHONE_LEAK_SENTINEL" not in blob
    assert "WEB_LEAK_SENTINEL" not in blob


# ── attach_brain_search ────────────────────────────────────
def _cfg():
    return {"model": {"provider": "openai", "model": "gpt-4o",
                      "messages": [{"role": "system", "content": "base"}]}}


def test_attach_adds_tool_and_guidance():
    cfg = vt.attach_brain_search(_cfg())
    names = [(t.get("function") or {}).get("name") for t in cfg["model"]["tools"]]
    assert "brain_search" in names
    guidance = cfg["model"]["messages"][0]["content"]
    assert "brain_search" in guidance
    # DA 反映: 雑談主目的の固定 + 数字を丸めない指示
    assert "雑談が主目的" in guidance
    assert "丸めず" in guidance


def test_attach_idempotent():
    cfg = vt.attach_brain_search(vt.attach_brain_search(_cfg()))
    names = [(t.get("function") or {}).get("name") for t in cfg["model"]["tools"]]
    assert names.count("brain_search") == 1
    assert cfg["model"]["messages"][0]["content"].count("brain_search ツール") == 1


# ── handle_tool_calls (payload 揺れ耐性) ────────────────────
class _StubIndex:
    def __init__(self, hits=None, raise_err=False):
        self.hits = hits if hits is not None else []
        self.raise_err = raise_err
        self.last_query = None

    async def search(self, query, n_results=6, collection="wiki", where=None):
        if self.raise_err:
            raise RuntimeError("chroma down")
        self.last_query = query
        return self.hits


class _StubBrain:
    def __init__(self, index):
        self.index = index


_HIT = {"content": "全店売上 週次合計 1,246,505,040 円。", "source": "wiki/knowledge/x.md",
        "metadata": {"title": "Monday Dash 最新"}, "distance": 0.2, "collection": "wiki"}


def test_tool_calls_happy_path_dict_args():
    brain = _StubBrain(_StubIndex(hits=[_HIT]))
    msg = {"toolCallList": [{"id": "tc1", "name": "brain_search",
                             "arguments": {"query": "先週の売上"}}]}
    out = _run(vt.handle_tool_calls(msg, brain))
    assert out["results"][0]["toolCallId"] == "tc1"
    assert out["results"][0]["name"] == "brain_search"  # spec required (ToolCallResult)
    assert "Monday Dash" in out["results"][0]["result"]
    assert brain.index.last_query == "先週の売上"


def test_tool_calls_string_args_and_function_nesting():
    """arguments が JSON 文字列 / function.name 入れ子 (現行 spec の OpenAI 形) でも解釈。"""
    brain = _StubBrain(_StubIndex(hits=[_HIT]))
    msg = {"toolCallList": [{"id": "tc2",
                             "function": {"name": "brain_search",
                                          "arguments": json.dumps({"query": "出店"})}}]}
    out = _run(vt.handle_tool_calls(msg, brain))
    assert "Monday Dash" in out["results"][0]["result"]


def test_tool_calls_unknown_tool_and_empty_query():
    brain = _StubBrain(_StubIndex(hits=[_HIT]))
    msg = {"toolCallList": [
        {"id": "a", "name": "evil_tool", "arguments": {"q": 1}},
        {"id": "b", "name": "brain_search", "arguments": {}},
    ]}
    out = _run(vt.handle_tool_calls(msg, brain))
    assert "unknown tool" in out["results"][0]["result"]
    assert out["results"][1]["toolCallId"] == "b"
    assert "検索語" in out["results"][1]["result"]


def test_tool_calls_search_error_does_not_raise():
    brain = _StubBrain(_StubIndex(raise_err=True))
    msg = {"toolCallList": [{"id": "c", "name": "brain_search",
                             "arguments": {"query": "x"}}]}
    out = _run(vt.handle_tool_calls(msg, brain))
    assert "エラー" in out["results"][0]["result"]


def test_no_index_and_no_brain_graceful():
    class NoIndexBrain:
        index = None
    out = _run(vt.search_brain_for_voice(NoIndexBrain(), "q"))
    assert "索引" in out
    # brain=None (app.state.brain 未初期化) でも同じ縮退
    out2 = _run(vt.search_brain_for_voice(None, "q"))
    assert "索引" in out2


# ── 音声向け整形 (DA 反映) ──────────────────────────────────
def test_search_dedups_sources_and_caps_length():
    hits = [dict(_HIT, source=f"wiki/k/{i % 2}.md",
                 content="あ" * 500) for i in range(8)]
    brain = _StubBrain(_StubIndex(hits=hits))
    out = _run(vt.search_brain_for_voice(brain, "q"))
    assert out.count("■") <= 2  # source dedup (0.md / 1.md の 2 種のみ)
    assert len(out) < 1200


def test_truncation_never_cuts_mid_number():
    """240 字 cut が数値の桁中に落ちる時、部分数字 (「1,246,」等) を残さない
    (LLM が確信を持って誤補完する音声数字事故の防止 = DA シナリオ2)。"""
    hits = [dict(_HIT, content=("あ" * 235) + "1,246,505,040円")]
    brain = _StubBrain(_StubIndex(hits=hits))
    out = _run(vt.search_brain_for_voice(brain, "q"))
    assert "1,246," not in out  # 桁途中の断片が出ない (数値ごと落ちる)


def test_formatting_strips_markdown_noise():
    hits = [dict(_HIT, content="## 見出し **強調** [[wikilink]] https://example.com/x |表|罫|")]
    brain = _StubBrain(_StubIndex(hits=hits))
    out = _run(vt.search_brain_for_voice(brain, "q"))
    for noise in ("##", "**", "[[", "]]", "https://", "|"):
        assert noise not in out


# ── main.py 配線の source pin ───────────────────────────────
def test_main_wiring_trusted_only_and_secret_gate():
    src = (_ROOT / "main.py").read_text(encoding="utf-8")
    # untrusted 縮退分岐 (早期 return) は attach より前にある = tool は trusted のみ
    i_untrusted = src.index("return _voice_align_assistant_dict(fm, system_prompt, server_secret)")
    i_attach = src.index("attach_brain_search(cfg)")
    assert i_untrusted < i_attach, "untrusted 早期 return が attach より前に無い"
    # tool-calls 分岐は phone / web いずれかの secret 必須 (どちらも webhook 冒頭で
    # compare_digest 済) + 深層 prompt を返す assistant-request は phone 限定のまま
    block = src[src.index('if mtype == "tool-calls"'):][:900]
    assert "is_phone_secret or is_web_secret" in block and "403" in block
    ar_block = src[src.index('if mtype == "assistant-request"'):][:600]
    assert "if not is_phone_secret:" in ar_block
