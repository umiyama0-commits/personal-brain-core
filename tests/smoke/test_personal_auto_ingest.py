"""smoke: Example 自動取込(両方=scrape主+export補完、2026-06-29)。

- export-watch: zip 検出 / 処理済 dedup / dry-run / 両 importer 呼び出し
- scrape dedup: write_personal_abstract が conv_id 既存なら skip(scrape↔export 二重取込防止)
- export PJ単位: title/本文に example 無くても PJ が Example なら取込(projects.json)
重い deps は不要(importer/llm は注入、playwright は claude_personal_sync の sync 内のみ)。
"""
import asyncio
import json
import pathlib
import sys
import zipfile

ROOT = pathlib.Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "scripts"))

import claude_export_watch as cew  # noqa: E402


def _make_export(tmp_path, name="export.zip", convos=None, projects=None):
    z = tmp_path / name
    with zipfile.ZipFile(z, "w") as zf:
        zf.writestr("conversations.json", json.dumps(convos or []))
        if projects is not None:
            zf.writestr("projects.json", json.dumps(projects))
    return z


def _make_nonexport(tmp_path):
    z = tmp_path / "other.zip"
    with zipfile.ZipFile(z, "w") as zf:
        zf.writestr("readme.txt", "hi")
    return z


def test_is_claude_export(tmp_path):
    assert cew._is_claude_export(_make_export(tmp_path)) is True
    assert cew._is_claude_export(_make_nonexport(tmp_path)) is False


def test_find_exports_only_claude(tmp_path):
    _make_export(tmp_path)
    _make_nonexport(tmp_path)
    found = cew.find_exports([tmp_path])
    assert len(found) == 1 and found[0].name == "export.zip"


def test_run_processes_then_skips(tmp_path, monkeypatch):
    monkeypatch.setattr(cew, "STATE_FILE", tmp_path / ".state.json")
    _make_export(tmp_path)
    calls = []
    async def fake_example(z):
        calls.append(("t", z.name)); return {"ok": True}
    async def fake_align(z):
        calls.append(("a", z.name)); return {"ok": True}
    r1 = asyncio.run(cew.run(dirs=[tmp_path], importers=(fake_example, fake_align)))
    assert r1["processed"] == 1 and len(calls) == 2      # 両 importer 呼ばれる
    r2 = asyncio.run(cew.run(dirs=[tmp_path], importers=(fake_example, fake_align)))
    assert r2["processed"] == 0                          # 2回目は処理済で skip


def test_dry_run_calls_nothing(tmp_path, monkeypatch):
    monkeypatch.setattr(cew, "STATE_FILE", tmp_path / ".state.json")
    _make_export(tmp_path)
    called = []
    async def fake(z):
        called.append(z); return {}
    r = asyncio.run(cew.run(dry_run=True, dirs=[tmp_path], importers=(fake, fake)))
    assert r["processed"] == 0 and called == []


def test_scrape_dedup_skips_existing_conv(tmp_path, monkeypatch):
    import claude_personal_sync as cts
    monkeypatch.setattr(cts, "DEST_DIR", tmp_path)
    p1 = cts.write_personal_abstract("abcdef1234567890", "植栽計画", "要約1")
    p2 = cts.write_personal_abstract("abcdef1234567890", "植栽計画 再", "要約2")  # 同 conv_id(別経路想定)
    assert p1 == p2                                      # dedup: 既存を返す
    assert len(list(tmp_path.glob("*.md"))) == 1         # 1 ファイルのみ
    assert "要約1" in p1.read_text(encoding="utf-8")     # 上書きしない


def test_export_catches_pj_conv_without_marker(tmp_path, monkeypatch):
    import claude_export_import as cei
    monkeypatch.setattr(cei, "_load_state", lambda: set())
    convos = [
        {"uuid": "c1", "name": "資金繰り計算", "project_uuid": "p-tol",   # title/本文に example 無し
         "chat_messages": [{"sender": "human", "text": "来月の支払いどうする"}]},
        {"uuid": "c2", "name": "OWNDAYS 売上", "project_uuid": "p-ow",
         "chat_messages": [{"sender": "human", "text": "今月の数字"}]},
    ]
    projects = [{"uuid": "p-tol", "name": "Example Garden"}, {"uuid": "p-ow", "name": "OWNDAYS"}]
    z = _make_export(tmp_path, convos=convos, projects=projects)
    async def fake_llm(*a, **k):
        return "要約"
    r = asyncio.run(cei.run(z, dry_run=True, llm=fake_llm))
    assert r["example_found"] == 1                        # Example PJ の c1 のみ、OWNDAYS は除外
