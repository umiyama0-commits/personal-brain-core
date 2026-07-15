"""tests/smoke/test_policy_diff_check.py — 開発方針 Fable5 最終チェック (★2026-07-10)。

lock する挙動:
1. 初回 run = state を HEAD に初期化のみ (LLM 0 call)
2. policy 外の commit のみ → state 前進のみ (LLM 0 call)
3. policy (CLAUDE.md) 変更 commit → supervisor レビュー → CONCERNS なら LINE 通知 (非critical)
4. LLM 失敗 → state 前進しない (翌日再試行) + loud_fail(ok=False)
5. REVIEW_PROMPT の .format が JSON 例の {{}} エスケープ込みで壊れない
"""
import json
import subprocess
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "scripts"))
import policy_diff_check as pdc  # noqa: E402


def _git(repo: Path, *args: str) -> None:
    subprocess.run(
        ["git", "-c", "user.email=t@t", "-c", "user.name=t", *args],
        cwd=repo, check=True, capture_output=True,
    )


@pytest.fixture()
def repo(tmp_path, monkeypatch):
    """tmp git repo + state/通知/loud_fail の隔離。"""
    r = tmp_path / "repo"
    (r / "docs" / "decisions").mkdir(parents=True)
    _git(r, "init", "-q")
    (r / "README.md").write_text("x", encoding="utf-8")
    _git(r, "add", "-A")
    _git(r, "commit", "-qm", "init")

    monkeypatch.setattr(pdc, "STATE_PATH", tmp_path / "state.json")
    pushes: list[tuple[str, dict]] = []
    monkeypatch.setattr(pdc, "line_push", lambda t, **kw: (pushes.append((t, kw)), True)[1])
    louds: list[tuple[str, bool]] = []
    monkeypatch.setattr(pdc, "loud_fail", lambda c, ok, *a, **kw: louds.append((c, ok)) or False)
    return r, pushes, louds


def test_first_run_initializes_only(repo, monkeypatch):
    r, pushes, louds = repo
    called = []
    monkeypatch.setattr(pdc, "call_llm", lambda *a, **kw: called.append(1))
    assert pdc.run(r) == 0
    st = json.loads(pdc.STATE_PATH.read_text(encoding="utf-8"))
    assert st["last_sha"] == pdc._head_sha(r)
    assert called == [] and pushes == []
    assert louds[-1] == ("policy_diff_check", True)


def test_non_policy_commit_advances_state_without_llm(repo, monkeypatch):
    r, pushes, _ = repo
    pdc.run(r)  # 初期化
    (r / "README.md").write_text("y", encoding="utf-8")
    _git(r, "add", "-A")
    _git(r, "commit", "-qm", "non-policy change")
    called = []
    monkeypatch.setattr(pdc, "call_llm", lambda *a, **kw: called.append(1))
    assert pdc.run(r) == 0
    st = json.loads(pdc.STATE_PATH.read_text(encoding="utf-8"))
    assert st["last_sha"] == pdc._head_sha(r)
    assert called == [] and pushes == []


def test_policy_change_reviews_and_notifies_noncritical(repo, monkeypatch):
    r, pushes, _ = repo
    pdc.run(r)  # 初期化
    (r / "CLAUDE.md").write_text("## 1. Discipline\n新ルール", encoding="utf-8")
    _git(r, "add", "-A")
    _git(r, "commit", "-qm", "policy: 新ルール追加")

    async def fake_review(commits, diff):
        assert any("policy" in c for c in commits)
        assert "新ルール" in diff
        return {"verdict": "CONCERNS",
                "findings": [{"severity": "high", "point": "矛盾あり", "quote": "新ルール"}]}

    monkeypatch.setattr(pdc, "review_with_supervisor", fake_review)
    assert pdc.run(r) == 0
    assert len(pushes) == 1
    text, kw = pushes[0]
    assert "開発方針 最終チェック" in text and "矛盾あり" in text
    assert kw.get("critical") is not True  # 方針レビューは非critical (LW に流さない)
    st = json.loads(pdc.STATE_PATH.read_text(encoding="utf-8"))
    assert st["last_sha"] == pdc._head_sha(r) and st["last_verdict"] == "CONCERNS"


def test_llm_failure_keeps_state_for_retry(repo, monkeypatch):
    r, pushes, louds = repo
    pdc.run(r)  # 初期化
    old_sha = json.loads(pdc.STATE_PATH.read_text(encoding="utf-8"))["last_sha"]
    (r / "docs" / "decisions" / "2026-07-10-x.md").write_text("ADR", encoding="utf-8")
    _git(r, "add", "-A")
    _git(r, "commit", "-qm", "ADR 追加")

    async def boom(commits, diff):
        raise RuntimeError("LLM down")

    monkeypatch.setattr(pdc, "review_with_supervisor", boom)
    assert pdc.run(r) == 1
    st = json.loads(pdc.STATE_PATH.read_text(encoding="utf-8"))
    assert st["last_sha"] == old_sha  # 前進していない = 翌日再試行
    assert louds[-1] == ("policy_diff_check", False)
    assert pushes == []


def test_review_prompt_format_is_valid():
    out = pdc.REVIEW_PROMPT.format(commits="abc fix", diff="+ x", cap=50000)
    assert '"verdict"' in out and "abc fix" in out and "+ x" in out
