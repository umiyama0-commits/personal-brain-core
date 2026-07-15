"""smoke: scripts/personal_snapshot.py — personal ドメインの版管理(保管)。git 無し環境は skip。"""
import pathlib
import shutil
import sys

import pytest

ROOT = pathlib.Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "scripts"))

import personal_snapshot  # noqa: E402


@pytest.mark.skipif(shutil.which("git") is None, reason="git 無し")
def test_snapshot_versions_personal(tmp_path):
    pd = tmp_path / "personal"
    pd.mkdir()
    (pd / "a.md").write_text("v1", encoding="utf-8")
    personal_snapshot.PERSONAL_DIR = pd

    r1 = personal_snapshot.snapshot("first")
    assert r1["ok"] and r1["changed"] is True

    # 変更無し → no-op
    assert personal_snapshot.snapshot()["changed"] is False

    # 変更 → 新 snapshot、履歴に両方
    (pd / "a.md").write_text("v2", encoding="utf-8")
    assert personal_snapshot.snapshot("second")["changed"] is True
    log = personal_snapshot.list_snapshots()
    assert "first" in log and "second" in log


@pytest.mark.skipif(shutil.which("git") is None, reason="git 無し")
def test_restore_recovers_prior_version(tmp_path):
    pd = tmp_path / "personal"
    pd.mkdir()
    (pd / "a.md").write_text("original", encoding="utf-8")
    personal_snapshot.PERSONAL_DIR = pd
    personal_snapshot.snapshot("v1")
    # commit hash を取得
    commit = personal_snapshot._git("rev-parse", "HEAD").stdout.strip()
    (pd / "a.md").write_text("corrupted", encoding="utf-8")
    personal_snapshot.snapshot("v2-bad")
    res = personal_snapshot.restore(commit)
    assert res["ok"]
    assert (pd / "a.md").read_text(encoding="utf-8") == "original"


def test_snapshot_no_personal_dir(tmp_path):
    personal_snapshot.PERSONAL_DIR = tmp_path / "nonexistent"
    r = personal_snapshot.snapshot()
    assert r["ok"] is False
