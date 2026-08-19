"""smoke: scripts/claude_export_import.py — Claude.ai export → Example だけ personal import。

LLM・network 非依存(llm 注入 + fixture export)。parse / Example フィルタ(title+content)/
zip+json 読込 / 非Example を保存しない / personal private 書込 を検証。
"""
import asyncio
import json
import pathlib
import sys
import zipfile

ROOT = pathlib.Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "scripts"))

import claude_export_import as cei  # noqa: E402
import claude_personal_sync as cts  # noqa: E402


def _export_data():
    return [
        {"uuid": "conv-example-1", "name": "Example Garden 戦略",
         "chat_messages": [{"sender": "human", "text": "現場暗黙知のオントロジー化 SECRET_T"},
                           {"sender": "assistant", "text": "了解です"}]},
        {"uuid": "conv-generic-2", "name": "今週の予定",  # 非Example(title も content も)
         "chat_messages": [{"sender": "human", "text": "OWNDAYS 売上の件 SECRET_O"}]},
        {"uuid": "conv-content-3", "name": "メモ",  # title は非Example だが content に Example
         "chat_messages": [{"sender": "human", "content": [{"type": "text",
            "text": "Example Gardenの資料を明石さんから受領。現場暗黙知の構造化方針を相談したい。"}]}]},
    ]


def test_parse_handles_both_message_formats():
    convos = cei.parse_conversations(_export_data())
    assert len(convos) == 3
    # 旧形式 text
    assert convos[0]["messages"][0]["content"] == "現場暗黙知のオントロジー化 SECRET_T"
    assert convos[0]["messages"][0]["role"] == "user"
    # 新形式 content[].text
    assert convos[2]["messages"][0]["content"].startswith("Example Gardenの資料")


def test_is_example_conv_title_and_content():
    convos = cei.parse_conversations(_export_data())
    assert cei.is_example_conv(convos[0]) is True   # title 合致
    assert cei.is_example_conv(convos[1]) is False  # 非Example
    assert cei.is_example_conv(convos[2]) is True   # content 合致(title は非Example)


def test_load_export_json_and_zip(tmp_path):
    j = tmp_path / "conversations.json"
    j.write_text(json.dumps(_export_data()), encoding="utf-8")
    assert len(cei.load_export(j)) == 3
    z = tmp_path / "export.zip"
    with zipfile.ZipFile(z, "w") as zf:
        zf.writestr("conversations.json", json.dumps(_export_data()))
    assert len(cei.load_export(z)) == 3


def test_run_imports_only_example_private(tmp_path, monkeypatch):
    dest = tmp_path / "wiki" / "personal" / "example-garden" / "conversations"
    monkeypatch.setattr(cts, "DEST_DIR", dest)
    monkeypatch.setattr(cei, "STATE_FILE", tmp_path / ".import_state.json")
    monkeypatch.setattr("subprocess.run", lambda *a, **k: None)   # snapshot 副作用を無効化
    j = tmp_path / "conversations.json"
    j.write_text(json.dumps(_export_data()), encoding="utf-8")

    async def fake_llm(prompt, **k):
        # prompt に会話本文が入っていることだけ確認しつつ要約を返す
        return "## 要点\n- " + ("オントロジー" if "オントロジー" in prompt else "資料")

    r = asyncio.run(cei.run(j, llm=fake_llm))
    assert r["example_found"] == 2 and r["written"] == 2   # Example 2件のみ(非Example 除外)
    files = list(dest.glob("*.md"))
    assert len(files) == 2
    blob = "\n".join(f.read_text(encoding="utf-8") for f in files)
    assert "clone_visibility: private" in blob            # 隔離
    assert "SECRET_O" not in blob                         # 非Example の本文は入らない

    # 再実行は state で重複 import しない
    r2 = asyncio.run(cei.run(j, llm=fake_llm))
    assert r2["written"] == 0
