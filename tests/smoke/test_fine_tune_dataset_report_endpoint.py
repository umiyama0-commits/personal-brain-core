"""smoke test: GET /api/fine-tune/dataset-report (★2026-05-23 海山指示)

Mac Studio 不要で fine-tune dataset の規模 + 品質を MacBook curl で即確認できる
endpoint の構造 sanity。
"""
from __future__ import annotations

from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent.parent


@pytest.mark.smoke
def test_dataset_report_endpoint_registered():
    src = (REPO / "routes" / "brain_api.py").read_text(encoding="utf-8")
    assert '@router.get("/api/fine-tune/dataset-report")' in src
    assert "async def fine_tune_dataset_report" in src


@pytest.mark.smoke
def test_dataset_report_token_protected():
    src = (REPO / "routes" / "brain_api.py").read_text(encoding="utf-8")
    fn_start = src.find("async def fine_tune_dataset_report")
    fn_end = src.find("@router.", fn_start + 100)
    fn_body = src[fn_start:fn_end]
    assert "check_at_token(token)" in fn_body


@pytest.mark.smoke
def test_dataset_report_imports_build_fine_tune_dataset():
    """build_fine_tune_dataset の collect_pairs + build_report を再利用する。"""
    src = (REPO / "routes" / "brain_api.py").read_text(encoding="utf-8")
    fn_start = src.find("async def fine_tune_dataset_report")
    fn_end = src.find("@router.", fn_start + 100)
    fn_body = src[fn_start:fn_end]
    assert "import build_fine_tune_dataset" in fn_body
    assert "collect_pairs" in fn_body
    assert "build_report" in fn_body


@pytest.mark.smoke
def test_dataset_report_validates_min_quality():
    """min_quality は 1-5 のみ受け付け、それ以外は 400。"""
    src = (REPO / "routes" / "brain_api.py").read_text(encoding="utf-8")
    fn_start = src.find("async def fine_tune_dataset_report")
    fn_end = src.find("@router.", fn_start + 100)
    fn_body = src[fn_start:fn_end]
    assert "min_quality must be 1-5" in fn_body
    assert "status_code=400" in fn_body


@pytest.mark.smoke
def test_dataset_report_returns_verdict_and_next_step():
    """件数別の verdict + next_step を含む。"""
    src = (REPO / "routes" / "brain_api.py").read_text(encoding="utf-8")
    fn_start = src.find("async def fine_tune_dataset_report")
    fn_end = src.find("@router.", fn_start + 100)
    fn_body = src[fn_start:fn_end]
    assert '"verdict"' in fn_body
    assert '"next_step"' in fn_body
    assert '"report_markdown"' in fn_body
    # 件数別判定
    assert "n < 100" in fn_body
    assert "n < 500" in fn_body
    assert "n < 2000" in fn_body
    # 推奨 model 名が含まれる
    assert "gpt-5.4-mini" in fn_body or "gpt-4o-mini" in fn_body


@pytest.mark.smoke
def test_dataset_report_returns_summary_structure():
    src = (REPO / "routes" / "brain_api.py").read_text(encoding="utf-8")
    fn_start = src.find("async def fine_tune_dataset_report")
    fn_end = src.find("@router.", fn_start + 100)
    fn_body = src[fn_start:fn_end]
    # response shape
    assert '"summary"' in fn_body
    assert '"by_source"' in fn_body
    assert '"by_quality"' in fn_body
    assert '"lengths"' in fn_body
    assert '"user_query"' in fn_body
    assert '"bot_response"' in fn_body
