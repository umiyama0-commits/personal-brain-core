"""tests/smoke/test_agent_core.py — run_agent agentic 化コア (★2026-07-20 個人エージェント評価 #1)。

services/agent_core.py: TOOLS↔executors 整合、bounded tool-loop (rounds/fallback/エラー)、
persona digest (frontmatter 除去/cache/graceful)、system prompt 構築、main.py wiring pin。
"""
from __future__ import annotations

import asyncio
import json
from pathlib import Path

import pytest

from services import agent_core as ac

_ROOT = Path(__file__).resolve().parents[2]


@pytest.fixture(autouse=True)
def _isolate(tmp_path, monkeypatch):
    monkeypatch.setenv("BRAIN_APP_ROOT", str(tmp_path))
    # persona cache を test 間で汚染させない
    ac._persona_cache["key"] = None
    ac._persona_cache["value"] = ""
    yield


# ─── TOOLS 整合 ───

def test_tools_all_have_executors():
    """TOOLS の全 tool 名に対し executor が揃う (内部 4 + 外部 5)。"""
    external = {n: (lambda a: "") for n in (
        "answer_business_question", "search_brain", "search_drive", "get_calendar", "get_mail")}
    ex = ac.merge_executors(external)
    tool_names = {t["function"]["name"] for t in ac.TOOLS}
    assert tool_names <= set(ex.keys()), f"executor 不足: {tool_names - set(ex.keys())}"


def test_internal_write_tools_execute(tmp_path):
    ex = ac.merge_executors({})
    out = asyncio.run(ac._exec_tool(ex, "add_task", {"text": "テスト作業"}))
    assert "タスク追加" in out
    out = asyncio.run(ac._exec_tool(ex, "complete_task", {"match": "テスト作業"}))
    assert "完了" in out
    out = asyncio.run(ac._exec_tool(ex, "remember", {"section": "facts", "text": "記憶項目"}))
    assert "記憶しました" in out
    out = asyncio.run(ac._exec_tool(ex, "create_reminder", {"date": "2099-02-01", "title": "T"}))
    assert "2099-02-01" in out


def test_exec_tool_unknown_and_error():
    def _boom(a):
        raise ValueError("bad")
    out = asyncio.run(ac._exec_tool({"x": _boom}, "x", {}))
    assert out.startswith("tool error")
    out = asyncio.run(ac._exec_tool({}, "nothere", {}))
    assert "unknown tool" in out


# ─── tool loop ───

def _resp(msg):
    class _R:
        def raise_for_status(self):
            pass

        def json(self):
            return {"choices": [{"message": msg}]}
    return _R()


class _SeqHttp:
    """post ごとに用意した message を順に返す fake。fail_at の call index (0-based) は raise。"""

    def __init__(self, messages, fail_at=()):
        self._msgs = list(messages)
        self.payloads = []
        self._fail_at = set(fail_at)
        self._n = 0

    async def post(self, url, **kw):
        idx = self._n
        self._n += 1
        self.payloads.append(kw.get("json"))
        if idx in self._fail_at:
            raise RuntimeError("simulated failure")
        return _resp(self._msgs.pop(0))


def test_loop_plain_answer_single_round():
    http = _SeqHttp([{"content": "こんにちは"}])
    out = asyncio.run(ac.run_tool_loop(http, "http://x", "k", "smart", [{"role": "user", "content": "hi"}], {}))
    assert out == "こんにちは"
    assert len(http.payloads) == 1
    assert "tools" in http.payloads[0]  # round0 は tools 付き


def test_loop_tool_call_then_answer():
    tc = {"id": "c1", "type": "function", "function": {"name": "echo", "arguments": json.dumps({"v": "A"})}}
    http = _SeqHttp([
        {"content": None, "tool_calls": [tc]},
        {"content": "ツール結果を踏まえた回答"},
    ])
    seen = []

    def _echo(a):
        seen.append(a)
        return "ECHO:" + a.get("v", "")
    out = asyncio.run(ac.run_tool_loop(http, "http://x", "k", "smart", [{"role": "user", "content": "q"}], {"echo": _echo}))
    assert out == "ツール結果を踏まえた回答"
    assert seen == [{"v": "A"}]
    # 2 回目の呼び出しに tool 結果 message が積まれている
    msgs2 = http.payloads[1]["messages"]
    assert any(m.get("role") == "tool" and "ECHO:A" in m.get("content", "") for m in msgs2)


def test_loop_max_rounds_forces_final_with_tool_choice_none():
    """★cross-check 3体一致: final round は tools を**残して** tool_choice='none' —
    Anthropic は tool_use/tool_result 履歴を含む request に tools 定義必須 (外すと 400)。"""
    tc = {"id": "c1", "type": "function", "function": {"name": "echo", "arguments": "{}"}}
    http = _SeqHttp([{"content": None, "tool_calls": [tc]}] * ac.MAX_ROUNDS + [{"content": "最終回答"}])
    out = asyncio.run(ac.run_tool_loop(http, "http://x", "k", "smart", [{"role": "user", "content": "q"}], {"echo": lambda a: "r"}))
    assert out == "最終回答"
    final = http.payloads[-1]
    assert "tools" in final, "final round が tools を落としている (Anthropic 400 経路)"
    assert final.get("tool_choice") == "none"


def test_loop_round0_failure_falls_back_to_plain_without_guidance():
    system = {"role": "system", "content": "SYS\n\n" + ac.TOOL_GUIDANCE}
    http = _SeqHttp([{"content": "従来挙動の回答"}], fail_at=(0,))
    out = asyncio.run(ac.run_tool_loop(http, "http://x", "k", "smart", [system, {"role": "user", "content": "q"}], {}))
    assert out == "従来挙動の回答"
    fallback = http.payloads[-1]
    # fallback は tools 無し + 指針除去 (tools 無し mode に「必ず実行してから報告」を残さない)
    assert "tools" not in fallback
    assert "ツール使用指針" not in fallback["messages"][0]["content"]
    assert "SYS" in fallback["messages"][0]["content"]


def test_loop_round1_failure_degrades_to_forced_answer():
    """★cross-check Reviewer: round≥1 の一時障害は raise でなく tool_choice='none' で degrade
    (書込 tool 実行済みなのにエラー文だけ返る不整合を回避)。"""
    tc = {"id": "c1", "type": "function", "function": {"name": "echo", "arguments": "{}"}}
    http = _SeqHttp([{"content": None, "tool_calls": [tc]}, {"content": "degrade 回答"}], fail_at=(1,))
    out = asyncio.run(ac.run_tool_loop(http, "http://x", "k", "smart", [{"role": "user", "content": "q"}], {"echo": lambda a: "r"}))
    assert out == "degrade 回答"
    final = http.payloads[-1]
    assert final.get("tool_choice") == "none" and "tools" in final
    # tool 結果は保持されている
    assert any(m.get("role") == "tool" for m in final["messages"])


def test_loop_in_round_budget_skips_tools(monkeypatch):
    """★cross-check 3体一致: budget は round 内 (各 tool 実行前) でも判定。"""
    # monotonic 系列: start=0 → round0 判定 1s (残あり) → tool 前 999s (超過) → round1 判定 (final)
    # ★global time を patch すると asyncio 内部も食うため、agent_core のモジュール参照だけ差し替え
    seq = [0.0, 1.0, 999.0]

    class _FakeTime:
        @staticmethod
        def monotonic():
            return seq.pop(0) if len(seq) > 1 else seq[0]
    monkeypatch.setattr(ac, "time", _FakeTime)
    tc = {"id": "c1", "type": "function", "function": {"name": "echo", "arguments": "{}"}}
    called = []
    http = _SeqHttp([{"content": None, "tool_calls": [tc]}, {"content": "回答"}])
    out = asyncio.run(ac.run_tool_loop(http, "http://x", "k", "smart", [{"role": "user", "content": "q"}], {"echo": lambda a: called.append(1) or "r"}))
    assert out == "回答"
    assert not called, "budget 超過後に tool が実行された"
    assert any(m.get("role") == "tool" and "budget exceeded" in m.get("content", "")
               for m in http.payloads[-1]["messages"])
    assert http.payloads[-1].get("tool_choice") == "none"


def test_loop_empty_content_uses_fallback_text():
    http = _SeqHttp([{"content": None}])
    out = asyncio.run(ac.run_tool_loop(http, "http://x", "k", "smart", [{"role": "user", "content": "q"}], {}))
    assert out == ac._EMPTY_REPLY_FALLBACK  # LINE の空 text 400 を防ぐ


# ─── persona digest ───

def _write_wiki(tmp_path, name, body, fm=True):
    d = tmp_path / "data" / "brain" / "wiki"
    d.mkdir(parents=True, exist_ok=True)
    content = (f"---\nupdated: 2026-01-01\n---\n{body}") if fm else body
    (d / name).write_text(content, encoding="utf-8")


def test_persona_digest_strips_frontmatter_and_bounds(tmp_path):
    _write_wiki(tmp_path, "identity.md", "# Identity\n価値観: 顧客第一")
    _write_wiki(tmp_path, "thinking.md", "# Thinking\nデータ先行")
    _write_wiki(tmp_path, "style.md", "# Style\n断定調")
    digest = ac.load_persona_digest()
    assert "顧客第一" in digest and "データ先行" in digest and "断定調" in digest
    assert "updated: 2026-01-01" not in digest
    assert len(digest) <= 2400


def test_persona_digest_missing_files_graceful():
    assert ac.load_persona_digest() == ""


def test_persona_digest_cache_invalidates_on_mtime(tmp_path):
    _write_wiki(tmp_path, "identity.md", "旧バージョン")
    assert "旧バージョン" in ac.load_persona_digest()
    import os
    p = tmp_path / "data" / "brain" / "wiki" / "identity.md"
    p.write_text("---\nx: 1\n---\n新バージョン", encoding="utf-8")
    os.utime(p, (p.stat().st_atime + 10, p.stat().st_mtime + 10))
    assert "新バージョン" in ac.load_persona_digest()


# ─── system prompt ───

def test_build_system_prompt_includes_layers(tmp_path):
    _write_wiki(tmp_path, "identity.md", "価値観: 本質主義")
    from services import owner_memory as om
    om.add_entry("facts", "定宿はサンプルホテル")
    sp = ac.build_system_prompt("\n\n## Wiki\n中身", "\nPATCH", "2026-07-20 12:00")
    # 層の存在 (★2026-07-20 正式名称化)
    assert "Umiyama AI Agent" in sp
    assert "人格ダイジェスト" in sp and "本質主義" in sp
    assert "恒久メモリー" in sp and "サンプルホテル" in sp
    # 既存の実事故由来ルールを維持
    assert "AIなのでファイルを読めません" in sp
    # 捏造抑止 + tool 指針
    assert "推測で作らない" in sp
    assert "ツール使用指針" in sp
    assert "実行せずに「設定しました」と言うのは禁止" in sp
    # patches / live_context が末尾合成
    assert "PATCH" in sp and "## Wiki" in sp


def test_build_system_prompt_empty_layers_omitted():
    sp = ac.build_system_prompt("", "", "2026-07-20 12:00")
    assert "人格ダイジェスト" not in sp
    assert "恒久メモリー" not in sp
    assert "重要ルール" in sp
