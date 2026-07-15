"""smoke test: POST /api/voice-align/backfill (★2026-05-23 海山指示)

Vapi call ID 直接指定で backfill する endpoint の構造 sanity。
"""
from __future__ import annotations

from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent.parent


@pytest.mark.smoke
def test_backfill_endpoint_registered():
    src = (REPO / "routes" / "brain_api.py").read_text(encoding="utf-8")
    assert '@router.post("/api/voice-align/backfill")' in src
    assert "async def voice_align_backfill" in src


@pytest.mark.smoke
def test_backfill_requires_token():
    src = (REPO / "routes" / "brain_api.py").read_text(encoding="utf-8")
    fn_start = src.find("async def voice_align_backfill")
    fn_end = src.find("@router.", fn_start + 100)
    fn_body = src[fn_start:fn_end]
    assert "check_at_token(token)" in fn_body


@pytest.mark.smoke
def test_backfill_503_when_vapi_key_missing():
    """VAPI_PRIVATE_API_KEY 未設定なら 503 + メッセージ。"""
    src = (REPO / "routes" / "brain_api.py").read_text(encoding="utf-8")
    fn_start = src.find("async def voice_align_backfill")
    fn_end = src.find("@router.", fn_start + 100)
    fn_body = src[fn_start:fn_end]
    assert "VAPI_PRIVATE_API_KEY" in fn_body
    assert "503" in fn_body
    assert ".env に追加" in fn_body or "docker restart" in fn_body


@pytest.mark.smoke
def test_backfill_validates_call_ids_list():
    """call_ids が list でない or 空なら 400。"""
    src = (REPO / "routes" / "brain_api.py").read_text(encoding="utf-8")
    fn_start = src.find("async def voice_align_backfill")
    fn_end = src.find("@router.", fn_start + 100)
    fn_body = src[fn_start:fn_end]
    assert "call_ids must be non-empty list" in fn_body
    assert "max 20" in fn_body  # 上限


@pytest.mark.smoke
def test_backfill_imports_vapi_backfill_module():
    """/app/scripts/vapi_backfill を import して fetch_call / extract_transcript / save_raw を使う。"""
    src = (REPO / "routes" / "brain_api.py").read_text(encoding="utf-8")
    fn_start = src.find("async def voice_align_backfill")
    fn_end = src.find("@router.", fn_start + 100)
    fn_body = src[fn_start:fn_end]
    assert "import vapi_backfill" in fn_body
    assert "vapi_backfill.fetch_call" in fn_body
    assert "vapi_backfill.extract_transcript" in fn_body
    assert "vapi_backfill.save_raw" in fn_body


@pytest.mark.smoke
def test_backfill_returns_summary_and_results():
    """response 構造: summary (total/saved/skipped/error) + results array。"""
    src = (REPO / "routes" / "brain_api.py").read_text(encoding="utf-8")
    fn_start = src.find("async def voice_align_backfill")
    fn_end = src.find("@router.", fn_start + 100)
    fn_body = src[fn_start:fn_end]
    assert '"summary"' in fn_body
    assert '"results"' in fn_body
    assert '"saved"' in fn_body
    assert '"skipped"' in fn_body
    assert '"next_step"' in fn_body


@pytest.mark.smoke
def test_backfill_auto_extract_option():
    """auto_extract=true で BackgroundTask 起動 (= 蒸留も同時に走る)。"""
    src = (REPO / "routes" / "brain_api.py").read_text(encoding="utf-8")
    fn_start = src.find("async def voice_align_backfill")
    fn_end = src.find("@router.", fn_start + 100)
    fn_body = src[fn_start:fn_end]
    # auto_extract 引数を body から読む
    assert "auto_extract" in fn_body
    # BackgroundTask で蒸留 helper を呼ぶ
    assert "bg_tasks.add_task" in fn_body
    assert "_extract_voice_raws" in fn_body
    # response に auto_extract_started フラグ
    assert "auto_extract_started" in fn_body


@pytest.mark.smoke
def test_extract_voice_raws_helper_exists():
    """_extract_voice_raws helper が存在し、alignment_interview.extract_session を呼ぶ。"""
    src = (REPO / "routes" / "brain_api.py").read_text(encoding="utf-8")
    assert "async def _extract_voice_raws" in src
    # 既存 alignment_interview module を再利用
    assert "import alignment_interview" in src
    assert "extract_session" in src
    # 既蒸留 skip ロジック
    assert "already extracted" in src or "skip" in src.lower()
