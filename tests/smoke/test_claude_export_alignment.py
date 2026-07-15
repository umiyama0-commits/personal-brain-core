"""smoke: scripts/claude_export_alignment.py — export → アラインメント雑談だけを人格蒸留へ。

dry_run は alignment_interview(重い)を import せず、parse/filter/project解決のみ検証(CI軽量・LLM非依存)。
"""
import asyncio
import json
import pathlib
import sys
import zipfile

ROOT = pathlib.Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "scripts"))

import claude_export_alignment as cea  # noqa: E402


def _export(tmp_path):
    convos = [
        {"uuid": "c-align-1", "name": "クローン育成チャット", "project_uuid": "p-align",
         "chat_messages": [{"sender": "human", "text": "自分の判断軸について話す。現場の数値は当て推量しない。"},
                           {"sender": "assistant", "text": "なるほど"}]},
        {"uuid": "c-other-2", "name": "OWNDAYS 売上の相談", "project_uuid": "p-work",
         "chat_messages": [{"sender": "human", "text": "今月の売上どう"}]},
        {"uuid": "c-title-3", "name": "アラインメント雑談 2026-06", "project_uuid": "p-work",
         "chat_messages": [{"sender": "human", "text": "価値観の話。人を大事にしたい。理屈で納得したい。"},
                           {"sender": "assistant", "text": "それは Fe を意識的に補う姿勢ですね"}]},
    ]
    projects = [{"uuid": "p-align", "name": "アラインメント雑談"}, {"uuid": "p-work", "name": "OWNDAYS"}]
    z = tmp_path / "export.zip"
    with zipfile.ZipFile(z, "w") as zf:
        zf.writestr("conversations.json", json.dumps(convos))
        zf.writestr("projects.json", json.dumps(projects))
    return z


def test_load_export_with_projects(tmp_path):
    convos, projects = cea.load_export_with_projects(_export(tmp_path))
    assert len(convos) == 3
    assert projects["p-align"] == "アラインメント雑談"


def test_project_name_resolution(tmp_path):
    convos, projects = cea.load_export_with_projects(_export(tmp_path))
    assert cea._project_name(convos[0], projects) == "アラインメント雑談"   # project_uuid 経由
    assert cea._project_name(convos[1], projects) == "OWNDAYS"


def test_is_alignment_conv_project_or_title():
    # project が アラインメント雑談 → 合致(title はクローン育成チャット)
    assert cea.is_alignment_conv({"title": "クローン育成チャット"}, "アラインメント雑談") is True
    # project が OWNDAYS でも title が アラインメント → 合致
    assert cea.is_alignment_conv({"title": "アラインメント雑談 2026-06"}, "OWNDAYS") is True
    # 両方とも非合致 → False(雑多な会話を巻き込まない)
    assert cea.is_alignment_conv({"title": "売上の相談"}, "OWNDAYS") is False


def test_to_transcript_format():
    t = cea._to_transcript([{"role": "user", "content": "あ"}, {"role": "assistant", "content": "い"}])
    assert "海山: あ" in t and "AI: い" in t


def test_run_dry_run_filters_only_alignment(tmp_path, monkeypatch):
    monkeypatch.setattr(cea, "STATE_FILE", tmp_path / ".state.json")
    z = _export(tmp_path)
    r = asyncio.run(cea.run(z, dry_run=True))
    # c-align-1(project 合致)+ c-title-3(title 合致)= 2件、OWNDAYS 売上(c-other-2)は除外
    assert r["matched"] == 2 and r["extracted"] == 0
