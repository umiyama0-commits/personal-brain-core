"""smoke test: clone_ab_test (online A/B test framework)."""
from __future__ import annotations

import importlib
import json
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(REPO_ROOT / "scripts"))


@pytest.fixture
def ab(tmp_path, monkeypatch):
    """AB_DIR を tmp_path に向け clone_ab_test を reload。"""
    monkeypatch.setenv("BRAIN_APP_ROOT", str(tmp_path))
    if "clone_ab_test" in sys.modules:
        importlib.reload(sys.modules["clone_ab_test"])
    import clone_ab_test  # type: ignore
    importlib.reload(clone_ab_test)  # IMPROVE_DIR を反映
    return clone_ab_test


@pytest.mark.smoke
def test_module_imports(ab):
    assert hasattr(ab, "create_experiment")
    assert hasattr(ab, "assign_bucket")
    assert hasattr(ab, "get_bucket_config")
    assert hasattr(ab, "analyze_experiment")
    assert hasattr(ab, "finalize_experiment")


@pytest.mark.smoke
def test_assign_bucket_control_when_no_experiment(ab):
    """active 実験が無ければ control を返す。"""
    bucket = ab.assign_bucket("user_xyz")
    assert bucket == "control"


@pytest.mark.smoke
def test_create_and_assign(ab):
    """実験作成後、user_id から決定論的に bucket が振られる。"""
    ab.create_experiment(
        "test-exp-1",
        bucket_a={"model": "smart"},
        bucket_b={"model": "smart-gpt"},
        description="test",
    )
    # 同じ user_id なら毎回同じ bucket
    b1 = ab.assign_bucket("user_alice", "test-exp-1")
    b2 = ab.assign_bucket("user_alice", "test-exp-1")
    assert b1 == b2
    assert b1 in ("A", "B")


@pytest.mark.smoke
def test_get_bucket_config_returns_model(ab):
    """bucket config に model が含まれる。"""
    ab.create_experiment(
        "test-exp-2",
        bucket_a={"model": "smart"},
        bucket_b={"model": "smart-gpt"},
    )
    # 多数の user で振り分けると A/B 両方出る
    buckets = set()
    for i in range(20):
        cfg = ab.get_bucket_config(f"user_{i}", "test-exp-2")
        if cfg["bucket"] in ("A", "B"):
            buckets.add(cfg["bucket"])
            assert "model" in cfg
    assert len(buckets) == 2  # A も B も出る


@pytest.mark.smoke
def test_finalize_removes_active(ab):
    ab.create_experiment("test-exp-3", {"model": "x"}, {"model": "y"})
    assert ab.get_active_experiment_id() == "test-exp-3"
    ok = ab.finalize_experiment("test-exp-3")
    assert ok is True
    # active から外れた
    assert ab.get_active_experiment_id() is None
    # 既存 experiment record は status=completed
    exp = ab.get_experiment("test-exp-3")
    assert exp["status"] == "completed"


@pytest.mark.smoke
def test_inactive_experiment_returns_control(ab):
    """completed 実験には control が返る。"""
    ab.create_experiment("test-exp-4", {"model": "a"}, {"model": "b"})
    ab.finalize_experiment("test-exp-4")
    b = ab.assign_bucket("user_xyz", "test-exp-4")
    assert b == "control"


@pytest.mark.smoke
def test_list_experiments(ab):
    ab.create_experiment("test-exp-5a", {"model": "a"}, {"model": "b"})
    ab.create_experiment("test-exp-5b", {"model": "c"}, {"model": "d"})
    exps = ab.list_experiments()
    ids = {e["id"] for e in exps}
    assert "test-exp-5a" in ids
    assert "test-exp-5b" in ids


@pytest.mark.smoke
def test_analyze_with_no_bot_events(ab, monkeypatch, tmp_path):
    """bot_events が空でも crash しない。"""
    monkeypatch.setenv("BRAIN_ROOT", str(tmp_path / "data" / "brain"))
    ab.create_experiment("test-exp-6", {"model": "a"}, {"model": "b"})
    report = ab.analyze_experiment("test-exp-6", days=7)
    # status が experiment_not_found ではなく 通常 report (n=0) になる
    assert "bucket_a" in report
    assert report["bucket_a"]["n_finished"] == 0


@pytest.mark.smoke
def test_assign_distribution_roughly_balanced(ab):
    """1000 user で振り分けて A/B が 30-70% の範囲に。"""
    ab.create_experiment("test-exp-7", {"model": "a"}, {"model": "b"})
    n_a, n_b = 0, 0
    for i in range(1000):
        bucket = ab.assign_bucket(f"u{i}", "test-exp-7")
        if bucket == "A":
            n_a += 1
        elif bucket == "B":
            n_b += 1
    # 30-70% に収まる (50% 期待)
    assert 300 <= n_a <= 700
    assert 300 <= n_b <= 700
    assert n_a + n_b == 1000
