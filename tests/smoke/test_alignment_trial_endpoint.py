"""smoke test: /alignment-trial/* endpoint (★2026-05-22 Phase 2 で routes/ に移管)。

main.py 全体は重い import (chromadb 等) を含むため、source 抽出で endpoint
ロジックの core 部分だけテスト。token 認証 + path traversal 防御を検証。

Phase 2 移管後: endpoint 本体は routes/alignment_trial.py、main.py には include_router のみ残る。
"""
from __future__ import annotations

import re
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent.parent


@pytest.mark.smoke
def test_endpoint_source_present():
    """routes/alignment_trial.py に /alignment-trial 系 endpoint が定義されている。"""
    src = (REPO_ROOT / "routes" / "alignment_trial.py").read_text(encoding="utf-8")
    # APIRouter 経由の path 定義
    assert '@router.get("/alignment-trial/{run_id}")' in src
    assert '@router.get("/alignment-trial/")' in src
    assert '@router.post("/alignment-trial/{run_id}/review")' in src
    assert '@router.post("/alignment-trial/run")' in src
    assert '@router.get("/alignment-trial/{run_id}/status")' in src
    # token 認証関数 (Phase 2 で _check_at_* から check_at_* に rename)
    assert "check_at_token" in src
    # path traversal 防御
    assert "check_at_run_id" in src
    # token env 取得
    assert "ALIGNMENT_TRIAL_TOKEN" in src
    assert "os.getenv" in src
    # remote run の background task ロジック
    assert "bg_tasks.add_task" in src
    assert "alignment_trial_run" in src
    assert "alignment_trial_status" in src
    # main.py 側に include_router が登録されている
    main_src = (REPO_ROOT / "main.py").read_text(encoding="utf-8")
    assert "from routes.alignment_trial import router" in main_src
    assert "app.include_router(_alignment_trial_router)" in main_src


@pytest.mark.smoke
def test_run_id_regex_blocks_traversal():
    """_RUN_ID_RE が path traversal を弾く (routes/alignment_trial.py 内)。"""
    src = (REPO_ROOT / "routes" / "alignment_trial.py").read_text(encoding="utf-8")
    m = re.search(r'_RUN_ID_RE\s*=\s*re\.compile\(r"([^"]+)"\)', src)
    assert m, "_RUN_ID_RE not found"
    pattern_str = m.group(1)
    pattern = re.compile(pattern_str)
    # 正常 (alphanumeric + dash + underscore)
    assert pattern.match("2026-05-21_run1")
    assert pattern.match("test-1")
    assert pattern.match("a_b_c")
    # 不正 (path traversal / 特殊文字)
    assert not pattern.match("../etc/passwd")
    assert not pattern.match("run1.html")  # . が含まれてる
    assert not pattern.match("run/1")       # / が含まれてる
    assert not pattern.match("run 1")       # space


@pytest.mark.smoke
def test_html_has_submit_buttons():
    """generate_html が「サーバ送信」「JSON ダウンロード」両方のボタンを出す。"""
    sys.path.insert(0, str(REPO_ROOT / "scripts"))
    if "clone_alignment_trial" in sys.modules:
        import importlib
        importlib.reload(sys.modules["clone_alignment_trial"])
    import clone_alignment_trial as mod  # type: ignore

    results = [
        {"id": "store-001", "role": "店長", "category": "TSA",
         "scenario": "s", "expected_axes": ["軸1"], "response": "r",
         "model": "smart", "ts": "2026-05-21T10:00:00"},
    ]
    html = mod.generate_html(results, tag="test")
    # 2 ボタン
    assert "submitToServer()" in html
    assert "exportReview()" in html
    # POST URL を組み立てる JavaScript
    assert "/alignment-trial/" in html
    assert "/review?token=" in html
    # 結果表示エリア
    assert 'id="result"' in html


@pytest.mark.smoke
def test_endpoint_url_pattern_in_html():
    """HTML 内の JavaScript が pathname から run_id を抽出する正規表現を持つ。"""
    sys.path.insert(0, str(REPO_ROOT / "scripts"))
    if "clone_alignment_trial" in sys.modules:
        import importlib
        importlib.reload(sys.modules["clone_alignment_trial"])
    import clone_alignment_trial as mod  # type: ignore

    results = [{"id": "x", "role": "x", "category": "x",
                "scenario": "s", "expected_axes": [], "response": "r",
                "model": "smart", "ts": "t"}]
    html = mod.generate_html(results, tag="test")
    # pathname.match で run_id 抽出
    assert "alignment-trial" in html
    assert "pathname" in html


@pytest.mark.smoke
def test_main_py_syntax_valid():
    """main.py が syntax error 無く parse 可能 (= endpoint 追加で壊してない)。"""
    import ast
    src = (REPO_ROOT / "main.py").read_text(encoding="utf-8")
    ast.parse(src)
