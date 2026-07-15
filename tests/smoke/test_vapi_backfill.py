"""smoke test: scripts/vapi_backfill.py (★2026-05-23 海山指示)

Vapi クレジット切れ期間 (5/20-23) の call transcript を遡及取り込みする script の
構造 sanity + 純粋関数の test。API 呼出は mock。
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(REPO / "scripts"))


@pytest.mark.smoke
def test_extract_transcript_from_artifact_string():
    """artifact.transcript (string) から抽出。"""
    from vapi_backfill import extract_transcript
    call = {"artifact": {"transcript": "海山: テスト\nAI: 返答"}}
    assert extract_transcript(call) == "海山: テスト\nAI: 返答"


@pytest.mark.smoke
def test_extract_transcript_from_messages():
    """artifact.messages array から「海山: ...」「AI: ...」整形。"""
    from vapi_backfill import extract_transcript
    call = {
        "artifact": {
            "messages": [
                {"role": "system", "message": "(system は除外)"},
                {"role": "user", "message": "最近どう?"},
                {"role": "assistant", "message": "元気だよ"},
                {"role": "customer", "message": "そっか"},
            ]
        }
    }
    out = extract_transcript(call)
    assert "(system は除外)" not in out  # system は除外
    assert "海山: 最近どう?" in out
    assert "AI: 元気だよ" in out
    assert "海山: そっか" in out  # customer も海山扱い


@pytest.mark.smoke
def test_extract_transcript_from_openai_format():
    """artifact.messagesOpenAIFormatted の content list 形式も対応。"""
    from vapi_backfill import extract_transcript
    call = {
        "artifact": {
            "messagesOpenAIFormatted": [
                {"role": "user", "content": [{"text": "Hello"}, {"text": "world"}]},
                {"role": "assistant", "content": "Hi there"},
            ]
        }
    }
    out = extract_transcript(call)
    assert "Hello world" in out
    assert "Hi there" in out


@pytest.mark.smoke
def test_extract_transcript_empty_returns_empty():
    from vapi_backfill import extract_transcript
    assert extract_transcript({}) == ""
    assert extract_transcript({"artifact": {}}) == ""
    assert extract_transcript({"artifact": {"messages": []}}) == ""


@pytest.mark.smoke
def test_save_raw_creates_file_with_correct_name(tmp_path, monkeypatch):
    """createdAt から YYYY-MM-DD-HHMM.md 形式で保存。"""
    monkeypatch.setenv("BRAIN_APP_ROOT", str(tmp_path))
    import importlib
    import vapi_backfill
    importlib.reload(vapi_backfill)

    call = {
        "id": "test_call_id",
        "createdAt": "2026-05-23T07:48:00.000Z",  # JST = 16:48
        "duration": 600,
        "endedReason": "customer-ended-call",
    }
    transcript = "海山: テスト発言\nAI: 返答内容"
    path = vapi_backfill.save_raw(call, transcript, dry_run=False)
    assert path is not None
    assert path.exists()
    # JST 変換確認: 07:48 UTC → 16:48 JST
    assert "2026-05-23-1648" in path.name


@pytest.mark.smoke
def test_save_raw_skips_existing(tmp_path, monkeypatch):
    """既存 file があれば save skip して None 返す。"""
    monkeypatch.setenv("BRAIN_APP_ROOT", str(tmp_path))
    import importlib
    import vapi_backfill
    importlib.reload(vapi_backfill)

    # 事前に file 作る
    raw_dir = tmp_path / "data" / "brain" / "raw" / "alignment_voice"
    raw_dir.mkdir(parents=True)
    (raw_dir / "2026-05-23-1648.md").write_text("既存", encoding="utf-8")

    call = {
        "id": "x", "createdAt": "2026-05-23T07:48:00.000Z",
        "duration": 0, "endedReason": "?",
    }
    result = vapi_backfill.save_raw(call, "transcript content", dry_run=False)
    assert result is None  # skip


@pytest.mark.smoke
def test_save_raw_writes_frontmatter(tmp_path, monkeypatch):
    """save された file に frontmatter (vapi_call_id / ended_reason 等) が入る。"""
    monkeypatch.setenv("BRAIN_APP_ROOT", str(tmp_path))
    import importlib
    import vapi_backfill
    importlib.reload(vapi_backfill)

    call = {
        "id": "call_xyz_123",
        "createdAt": "2026-05-22T05:00:00.000Z",
        "duration": 720,
        "endedReason": "customer-ended-call",
    }
    path = vapi_backfill.save_raw(call, "海山: テスト" * 30, dry_run=False)
    assert path is not None
    txt = path.read_text(encoding="utf-8")
    assert "source: vapi_backfill" in txt
    assert "vapi_call_id: call_xyz_123" in txt
    assert "ended_reason: customer-ended-call" in txt
    assert "duration_sec: 720" in txt


@pytest.mark.smoke
def test_main_exits_when_vapi_key_missing(monkeypatch):
    """VAPI_PRIVATE_API_KEY 未設定なら 1 で exit。"""
    monkeypatch.delenv("VAPI_PRIVATE_API_KEY", raising=False)
    import importlib
    import vapi_backfill
    importlib.reload(vapi_backfill)

    # main() を引数なしで呼ぶ
    monkeypatch.setattr("sys.argv", ["vapi_backfill.py", "--since", "2026-05-20"])
    rc = vapi_backfill.main()
    assert rc == 1
