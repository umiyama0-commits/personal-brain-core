"""smoke test: clone_improve_lib の各 helper。"""
from __future__ import annotations

import importlib
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest


@pytest.mark.smoke
def test_extract_json_codeblock():
    """LLM の ```json ... ``` パターンから JSON 抽出できる。"""
    import scripts.clone_improve_lib as lib

    text = """応答:
```json
{"date": "2026-05-20", "count": 5}
```
以上です。"""
    d = lib.extract_json(text)
    assert d == {"date": "2026-05-20", "count": 5}


@pytest.mark.smoke
def test_extract_json_bare():
    """コードブロックなしの bare JSON も抽出できる。"""
    import scripts.clone_improve_lib as lib

    text = '{"foo": "bar", "n": 1}'
    assert lib.extract_json(text) == {"foo": "bar", "n": 1}


@pytest.mark.smoke
def test_extract_json_invalid_raises():
    """JSON 含まない時は ValueError。"""
    import scripts.clone_improve_lib as lib

    with pytest.raises(ValueError):
        lib.extract_json("これは普通のテキスト")


@pytest.mark.smoke
def test_load_conversations(brain_root, sample_clone_history, monkeypatch):
    """clone_history から since 以降の record をロードできる。"""
    # brain_root fixture が既に BRAIN_APP_ROOT を tmp_path に設定済
    import scripts.clone_improve_lib as lib
    importlib.reload(lib)

    since = datetime(2026, 5, 20, 0, 0, tzinfo=timezone(timedelta(hours=9)))
    records = lib.load_conversations(since)
    # alice 3 + bob 3 = 6
    assert len(records) == 6
    # 時系列順
    for i in range(len(records) - 1):
        assert records[i].get("timestamp") <= records[i+1].get("timestamp")


@pytest.mark.smoke
def test_load_conversations_filters_old(brain_root, sample_clone_history, monkeypatch):
    """since より古い record はフィルタされる。"""
    # brain_root fixture が既に BRAIN_APP_ROOT を tmp_path に設定済
    import scripts.clone_improve_lib as lib
    importlib.reload(lib)

    # サンプルは 2026-05-20 9:00 以降、それより未来を since にすると 0 件
    since = datetime(2026, 5, 21, 0, 0, tzinfo=timezone(timedelta(hours=9)))
    records = lib.load_conversations(since)
    assert len(records) == 0


@pytest.mark.smoke
def test_group_by_session_separates_users(brain_root, sample_clone_history, monkeypatch):
    """user_id 別に session が分かれる。"""
    # brain_root fixture が既に BRAIN_APP_ROOT を tmp_path に設定済
    import scripts.clone_improve_lib as lib
    importlib.reload(lib)

    since = datetime(2026, 5, 20, 0, 0, tzinfo=timezone(timedelta(hours=9)))
    records = lib.load_conversations(since)
    sessions = lib.group_by_session(records, gap_minutes=30)
    # 各 user は 30 分以内の連続発言なので 1 session ずつ = 2 sessions
    assert len(sessions) == 2


@pytest.mark.smoke
def test_group_by_session_splits_on_gap(brain_root, monkeypatch):
    """gap_minutes 超えで session が分割される。"""
    # brain_root fixture が既に BRAIN_APP_ROOT を tmp_path に設定済
    import scripts.clone_improve_lib as lib
    importlib.reload(lib)

    # alice: 1 件目 + 60分後にもう 1 件 (gap=30 なら 2 session に分割される)
    base = datetime(2026, 5, 20, 10, 0, tzinfo=timezone(timedelta(hours=9)))
    hdir = brain_root / "clone_history"
    hdir.mkdir(parents=True, exist_ok=True)
    lines = [
        json.dumps({"timestamp": base.isoformat(), "user_id": "u1", "role": "user", "text": "a"}, ensure_ascii=False),
        json.dumps({"timestamp": (base + timedelta(hours=1)).isoformat(), "user_id": "u1", "role": "user", "text": "b"}, ensure_ascii=False),
    ]
    (hdir / "u1.jsonl").write_text("\n".join(lines), encoding="utf-8")
    records = lib.load_conversations(base - timedelta(days=1))
    sessions = lib.group_by_session(records, gap_minutes=30)
    assert len(sessions) == 2  # 60 分空けたので分かれる


@pytest.mark.smoke
def test_append_jsonl(tmp_path):
    """append_jsonl で 1 行ずつ追記される、再読み込みで全件取れる。"""
    import scripts.clone_improve_lib as lib

    p = tmp_path / "log.jsonl"
    lib.append_jsonl(p, {"a": 1})
    lib.append_jsonl(p, {"b": 2, "ts": "2026-05-20"})

    records = lib.read_jsonl(p)
    assert len(records) == 2
    assert records[0] == {"a": 1}
    assert records[1]["b"] == 2


@pytest.mark.smoke
def test_read_jsonl_missing_returns_empty():
    """存在しないファイルは空 list を返す。"""
    import scripts.clone_improve_lib as lib

    assert lib.read_jsonl(Path("/tmp/nonexistent-xyz-12345.jsonl")) == []
