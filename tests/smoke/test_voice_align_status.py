"""smoke test: /api/voice-align/status (★2026-05-23 海山質問対応)

Voice alignment pipeline (Vapi → raw → 蒸留 → wiki/interview) の各層 file 状況を
1 curl で診断できる endpoint の構造確認。
"""
from __future__ import annotations

from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent.parent


@pytest.mark.smoke
def test_voice_align_status_endpoint_registered():
    """routes/brain_api.py に GET /api/voice-align/status が登録されている。"""
    src = (REPO / "routes" / "brain_api.py").read_text(encoding="utf-8")
    assert '@router.get("/api/voice-align/status")' in src
    assert "async def voice_align_status" in src


@pytest.mark.smoke
def test_voice_align_status_token_protected():
    src = (REPO / "routes" / "brain_api.py").read_text(encoding="utf-8")
    # voice-align/status の関数内に check_at_token(token) がある
    fn_start = src.find("async def voice_align_status")
    fn_end = src.find("async def ", fn_start + 100)
    fn_body = src[fn_start:fn_end] if fn_end > 0 else src[fn_start:]
    assert "check_at_token(token)" in fn_body


@pytest.mark.smoke
def test_voice_align_status_reads_three_layers():
    """raw / extracted / wiki の 3 層を読む。"""
    src = (REPO / "routes" / "brain_api.py").read_text(encoding="utf-8")
    fn_start = src.find("async def voice_align_status")
    fn_end = src.find("async def ", fn_start + 100)
    fn_body = src[fn_start:fn_end]
    # 3 層全ての dir 参照
    assert "raw" in fn_body and "alignment_voice" in fn_body
    assert "interview_extracted" in fn_body
    assert "wiki" in fn_body and "interview" in fn_body
    # coverage JSON も読む
    assert "interview_coverage.json" in fn_body


@pytest.mark.smoke
def test_voice_align_status_includes_diagnosis():
    """raw と extracted の時刻比較で「蒸留詰まり」を診断する logic がある。"""
    src = (REPO / "routes" / "brain_api.py").read_text(encoding="utf-8")
    fn_start = src.find("async def voice_align_status")
    fn_end = src.find("async def ", fn_start + 100)
    fn_body = src[fn_start:fn_end]
    # diagnosis フィールド
    assert "diagnosis" in fn_body
    # 詰まり判定の文言
    assert "蒸留 pipeline が詰まっている" in fn_body or "蒸留 pipeline" in fn_body
