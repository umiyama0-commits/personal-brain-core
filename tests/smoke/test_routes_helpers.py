"""smoke test: routes/ helpers (★2026-05-22 Phase 2)。

fastapi 環境が無い test env でも回せるよう、helper function だけ test。
APIRouter の statefulness は docker 経由の本番 deploy で smoke する。
"""
from __future__ import annotations

import pytest

# fastapi 非依存の確認 (= helper function を ImportError なく呼べる) は
# import 時に fastapi が無くても module 単位で fail するので、conditional skip にする
fastapi_available = True
try:
    import fastapi  # noqa: F401
except Exception:
    fastapi_available = False


@pytest.mark.smoke
@pytest.mark.skipif(not fastapi_available, reason="fastapi 未インストール環境では skip")
def test_alignment_trial_router_exists():
    from routes.alignment_trial import router
    assert router is not None
    # 5 endpoint 登録されてる
    paths = [r.path for r in router.routes]
    assert "/alignment-trial/" in paths
    assert "/alignment-trial/{run_id}" in paths
    assert "/alignment-trial/{run_id}/review" in paths
    assert "/alignment-trial/run" in paths
    assert "/alignment-trial/{run_id}/status" in paths


@pytest.mark.smoke
@pytest.mark.skipif(not fastapi_available, reason="fastapi 未インストール環境では skip")
def test_brain_api_router_exists():
    from routes.brain_api import router
    assert router is not None
    paths = [r.path for r in router.routes]
    assert "/api/cost-investigation" in paths
    assert "/api/recent-failures" in paths


@pytest.mark.smoke
@pytest.mark.skipif(not fastapi_available, reason="fastapi 未インストール環境では skip")
def test_check_at_token_valid():
    from routes.alignment_trial import check_at_token, ALIGNMENT_TRIAL_TOKEN
    # 正しい token なら例外なし
    check_at_token(ALIGNMENT_TRIAL_TOKEN)


@pytest.mark.smoke
@pytest.mark.skipif(not fastapi_available, reason="fastapi 未インストール環境では skip")
def test_check_at_token_invalid():
    from fastapi import HTTPException
    from routes.alignment_trial import check_at_token
    with pytest.raises(HTTPException) as exc_info:
        check_at_token("wrong-token")
    assert exc_info.value.status_code == 403


@pytest.mark.smoke
@pytest.mark.skipif(not fastapi_available, reason="fastapi 未インストール環境では skip")
def test_check_at_run_id_invalid_path_traversal():
    from fastapi import HTTPException
    from routes.alignment_trial import check_at_run_id
    # path traversal 防止: dot や slash を含むと 400
    with pytest.raises(HTTPException) as exc:
        check_at_run_id("../etc/passwd")
    assert exc.value.status_code == 400
    with pytest.raises(HTTPException):
        check_at_run_id("foo/bar")


@pytest.mark.smoke
@pytest.mark.skipif(not fastapi_available, reason="fastapi 未インストール環境では skip")
def test_check_at_run_id_valid():
    from routes.alignment_trial import check_at_run_id
    # 正常 run_id (alphanumeric + dash/underscore)
    check_at_run_id("2026-05-21_run1")
    check_at_run_id("test123")
    check_at_run_id("abc_DEF-456")


# ─── main.py の側の include_router が登録されてるか (静的 check) ─────────────
@pytest.mark.smoke
def test_main_py_includes_routes():
    """main.py に include_router が書かれてる事を grep 相当で確認 (= deploy 漏れ防止)。"""
    from pathlib import Path
    src = Path(__file__).resolve().parent.parent.parent / "main.py"
    txt = src.read_text(encoding="utf-8")
    assert "from routes.alignment_trial import router" in txt
    assert "from routes.brain_api import router" in txt
    assert "app.include_router(_alignment_trial_router)" in txt
    assert "app.include_router(_brain_api_router)" in txt


@pytest.mark.smoke
def test_main_py_old_endpoints_removed():
    """main.py から旧 /alignment-trial/run + /api/cost-investigation 等が消えてる。"""
    from pathlib import Path
    src = Path(__file__).resolve().parent.parent.parent / "main.py"
    txt = src.read_text(encoding="utf-8")
    # 旧 inline endpoint 定義は無い
    assert '@app.post("/alignment-trial/run")' not in txt
    assert '@app.get("/api/cost-investigation")' not in txt
    assert '@app.get("/api/recent-failures")' not in txt
    assert '@app.get("/alignment-trial/{run_id}/status")' not in txt
    assert '@app.post("/alignment-trial/{run_id}/review")' not in txt
