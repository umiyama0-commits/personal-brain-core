"""smoke test: /api/prompt-diff/run + /api/prompt-diff/latest (★2026-05-23 海山指示 C)

Mac Studio 不要で brain.example.com 経由 prompt_diff_check を発火する endpoint の
構造 sanity check。実 LLM 呼出は本番でのみ起きる、ここは静的検証のみ。
"""
from __future__ import annotations

from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent.parent


@pytest.mark.smoke
def test_prompt_diff_endpoints_registered_in_routes_brain_api():
    """routes/brain_api.py に 2 endpoint が登録されている。"""
    src = (REPO / "routes" / "brain_api.py").read_text(encoding="utf-8")
    # POST /api/prompt-diff/run
    assert '@router.post("/api/prompt-diff/run")' in src
    assert "async def prompt_diff_run" in src
    # GET /api/prompt-diff/latest
    assert '@router.get("/api/prompt-diff/latest")' in src
    assert "async def prompt_diff_latest" in src


@pytest.mark.smoke
def test_prompt_diff_run_uses_background_task():
    """run endpoint が BackgroundTask で非同期実行する (= request response は即返す)。"""
    src = (REPO / "routes" / "brain_api.py").read_text(encoding="utf-8")
    # BackgroundTasks 引数を取る
    assert "BackgroundTasks" in src
    assert "bg_tasks.add_task" in src
    # clone_prompt_diff_check.main を呼ぶ
    assert "clone_prompt_diff_check" in src


@pytest.mark.smoke
def test_prompt_diff_run_validates_trigger_sha():
    """trigger_sha は alphanumeric + _- のみ (= path traversal 防止)。"""
    src = (REPO / "routes" / "brain_api.py").read_text(encoding="utf-8")
    # 検証ロジックがある (= isalnum or _-)
    assert "isalnum() or c in" in src
    # 不正なら 400
    assert 'status_code=400' in src
    assert "invalid trigger_sha" in src


@pytest.mark.smoke
def test_prompt_diff_endpoints_protected_by_token():
    """両 endpoint で check_at_token を呼ぶ (= ALIGNMENT_TRIAL_TOKEN 認証)。"""
    src = (REPO / "routes" / "brain_api.py").read_text(encoding="utf-8")
    # prompt_diff_run / prompt_diff_latest 両方で check_at_token(token)
    n = src.count("check_at_token(token)")
    assert n >= 4, f"check_at_token 呼出 {n} 回 < 4 (cost-investigation + recent-failures + 2 つの diff endpoint)"


@pytest.mark.smoke
def test_prompt_diff_latest_returns_file_metadata():
    """latest endpoint が file メタ (= name / mtime) と report 中身を返す。"""
    src = (REPO / "routes" / "brain_api.py").read_text(encoding="utf-8")
    # diff-*.json glob で最新を取る
    assert "diff-*.json" in src or "diff-{trigger_sha" in src
    # mtime を JST で返す
    assert "tz=JST" in src
    # latest_report の return shape
    assert '"file"' in src
    assert '"mtime"' in src
    assert '"report"' in src
