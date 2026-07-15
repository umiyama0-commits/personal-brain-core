"""smoke: scripts/claude_personal_sync.py — Example 会話だけ→personal の filter/abstract/書込。

Playwright(live scrape)非依存。title フィルタ・要約(llm 注入)・personal private 書込を検証。
"""
import asyncio
import pathlib
import sys

ROOT = pathlib.Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "scripts"))

import claude_personal_sync as cts  # noqa: E402


def test_is_example_title_filter():
    assert cts.is_example_title("Example Garden 戦略")
    assert cts.is_example_title("Example Gardenの件")
    assert cts.is_example_title("example garden notes")
    # 非Example は False(= 開かない=保存しない)
    assert not cts.is_example_title("OWNDAYS 売上分析")
    assert not cts.is_example_title("週末の予定")
    assert not cts.is_example_title("")


def test_abstract_grounded(monkeypatch):
    seen = {}

    async def fake_llm(prompt, **k):
        seen["prompt"] = prompt
        return "## 要点\n- 現場暗黙知のオントロジー化が目的\n## 次アクション\n- 資料整理"

    msgs = [{"role": "user", "content": "Exampleの目的は現場暗黙知のオントロジー化"},
            {"role": "assistant", "content": "承知しました。"}]
    out = asyncio.run(cts.abstract_conversation("Example Garden", msgs, llm=fake_llm))
    assert "オントロジー化" in out
    # 捏造禁止ガード + 会話本文が prompt に入る
    assert "創作・推測で補わない" in seen["prompt"]
    assert "現場暗黙知" in seen["prompt"]


def test_write_personal_abstract_is_private_and_in_personal(tmp_path, monkeypatch):
    dest = tmp_path / "wiki" / "personal" / "example-garden" / "conversations"
    monkeypatch.setattr(cts, "DEST_DIR", dest)
    p = cts.write_personal_abstract("abcd1234-conv-id", "Example 戦略会議", "## 要点\n- テスト SECRET_T")
    body = p.read_text(encoding="utf-8")
    assert "clone_visibility: private" in body        # 社員クローン非露出
    assert "project: example-garden" in body
    assert "SECRET_T" in body
    # 必ず personal/example-garden/ 配下 (= OWNDAYS 全経路から除外)
    rel = p.resolve().relative_to(tmp_path.resolve())
    assert str(rel).startswith("wiki/personal/example-garden/")


def test_empty_conversation_yields_no_abstract():
    out = asyncio.run(cts.abstract_conversation("Example", [{"role": "user", "content": "hi"}],
                                                llm=None))
    # 40字未満 → LLM 呼ばず空(call_llm 未注入でも例外にならない)
    assert out == ""
