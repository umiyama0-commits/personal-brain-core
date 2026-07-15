"""smoke test: clone_prompt_diff_check の純粋ロジック部分。

実 regression / LLM 呼び出しは skip、compare_qs / diff_report の
JSON 比較ロジックのみカバー。
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(REPO_ROOT / "scripts"))


@pytest.mark.smoke
def test_compare_qs_no_change():
    """pre/post 同一なら degraded=False。"""
    from clone_prompt_diff_check import compare_qs

    q = {"id": "Q1", "question": "テスト", "cosine": 0.85, "judge_score": 7, "violations": []}
    r = compare_qs(q, q.copy())
    assert r["degraded"] is False
    assert r["cos_delta"] == 0.0
    assert r["judge_delta"] == 0.0
    assert r["viol_delta"] == 0


@pytest.mark.smoke
def test_compare_qs_cosine_degraded():
    """cosine が 0.15 以上低下したら degraded。"""
    from clone_prompt_diff_check import compare_qs

    pre = {"id": "Q1", "question": "test", "cosine": 0.90, "judge_score": 8, "violations": []}
    post = {"id": "Q1", "question": "test", "cosine": 0.70, "judge_score": 8, "violations": []}
    r = compare_qs(pre, post)
    assert r["degraded"] is True
    assert any("cosine" in s for s in r["degraded_reasons"])


@pytest.mark.smoke
def test_compare_qs_judge_degraded():
    """judge が 1.5 以上低下したら degraded。"""
    from clone_prompt_diff_check import compare_qs

    pre = {"id": "Q5", "question": "test", "cosine": 0.85, "judge_score": 8, "violations": []}
    post = {"id": "Q5", "question": "test", "cosine": 0.85, "judge_score": 6, "violations": []}
    r = compare_qs(pre, post)
    assert r["degraded"] is True
    assert any("judge" in s for s in r["degraded_reasons"])


@pytest.mark.smoke
def test_compare_qs_violations_increase():
    """違反が 2 件以上増えたら degraded。"""
    from clone_prompt_diff_check import compare_qs

    pre = {"id": "Q11", "question": "test", "cosine": 0.85, "judge_score": 8, "violations": []}
    post = {
        "id": "Q11", "question": "test", "cosine": 0.85, "judge_score": 8,
        "violations": [{"pattern": "p1"}, {"pattern": "p2"}],
    }
    r = compare_qs(pre, post)
    assert r["degraded"] is True
    assert any("violations" in s for s in r["degraded_reasons"])


@pytest.mark.smoke
def test_compare_qs_improvement_not_degraded():
    """改善 (cosine 上昇 / judge 上昇 / violations 減) は degraded じゃない。"""
    from clone_prompt_diff_check import compare_qs

    pre = {
        "id": "Q12", "question": "test", "cosine": 0.60, "judge_score": 5,
        "violations": [{"pattern": "a"}, {"pattern": "b"}],
    }
    post = {
        "id": "Q12", "question": "test", "cosine": 0.85, "judge_score": 8,
        "violations": [],
    }
    r = compare_qs(pre, post)
    assert r["degraded"] is False
    assert r["cos_delta"] > 0
    assert r["judge_delta"] > 0
    assert r["viol_delta"] < 0


@pytest.mark.smoke
def test_diff_report_aggregate(tmp_path):
    """diff_report が 2 つの JSON を読んで集計できる。"""
    from clone_prompt_diff_check import diff_report

    pre = {
        "summary": {"avg_cosine": 0.8},
        "questions": [
            {"id": "Q1", "question": "a", "cosine": 0.85, "judge_score": 8, "violations": []},
            {"id": "Q2", "question": "b", "cosine": 0.90, "judge_score": 7, "violations": []},
            {"id": "Q3", "question": "c", "cosine": 0.75, "judge_score": 6, "violations": []},
        ],
    }
    post = {
        "summary": {"avg_cosine": 0.7},
        "questions": [
            {"id": "Q1", "question": "a", "cosine": 0.65, "judge_score": 8, "violations": []},  # cosine 劣化
            {"id": "Q2", "question": "b", "cosine": 0.90, "judge_score": 5, "violations": []},  # judge 劣化
            {"id": "Q3", "question": "c", "cosine": 0.75, "judge_score": 6, "violations": [{"p": "x"}, {"p": "y"}]},  # violations 増
        ],
    }
    pre_path = tmp_path / "pre.json"
    post_path = tmp_path / "post.json"
    pre_path.write_text(json.dumps(pre), encoding="utf-8")
    post_path.write_text(json.dumps(post), encoding="utf-8")

    r = diff_report(pre_path, post_path)
    assert r["n_compared"] == 3
    assert r["n_degraded"] == 3
    assert r["avg_cosine_delta"] < 0  # 劣化方向


@pytest.mark.smoke
def test_diff_report_missing_qid(tmp_path):
    """片方にしか無い Q は比較から除外される。"""
    from clone_prompt_diff_check import diff_report

    pre = {"questions": [{"id": "Q1", "question": "a", "cosine": 0.8, "judge_score": 7, "violations": []}]}
    post = {"questions": [{"id": "Q2", "question": "b", "cosine": 0.8, "judge_score": 7, "violations": []}]}
    pre_path = tmp_path / "pre.json"
    post_path = tmp_path / "post.json"
    pre_path.write_text(json.dumps(pre), encoding="utf-8")
    post_path.write_text(json.dumps(post), encoding="utf-8")

    r = diff_report(pre_path, post_path)
    assert r["n_compared"] == 0  # 共通 Q なし


@pytest.mark.smoke
def test_find_latest_pre_deploy_excludes_post_deploy(tmp_path, monkeypatch):
    """post-deploy- / pre-deploy- prefix は baseline 候補から除外される。"""
    from clone_prompt_diff_check import find_latest_pre_deploy
    import clone_prompt_diff_check as mod

    monkeypatch.setattr(mod, "REGRESSION_DIR", tmp_path)
    (tmp_path / "2026-05-20.json").write_text("{}")
    (tmp_path / "post-deploy-abc-2026-05-21.json").write_text("{}")
    (tmp_path / "diff-abc-2026-05-21.json").write_text("{}")

    r = find_latest_pre_deploy()
    # post-deploy- も diff- も baseline じゃない、nightly の 2026-05-20.json が選ばれる
    assert r is not None
    assert "post-deploy" not in r.name
    assert "diff-" not in r.name
    assert r.name == "2026-05-20.json"


@pytest.mark.smoke
def test_find_latest_pre_deploy_empty(tmp_path, monkeypatch):
    """REGRESSION_DIR 空なら None。"""
    from clone_prompt_diff_check import find_latest_pre_deploy
    import clone_prompt_diff_check as mod

    monkeypatch.setattr(mod, "REGRESSION_DIR", tmp_path)
    assert find_latest_pre_deploy() is None
