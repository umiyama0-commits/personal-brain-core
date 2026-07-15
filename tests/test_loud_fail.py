"""tests/test_loud_fail.py — clone_improve_lib.loud_fail (§1.18 loud-fail 標準) の単体テスト。

★2026-07-02 監査バッチC。threshold / cooldown / 成功リセット / component 独立 / state 破損 degrade /
通知失敗時の再試行 (last_alert 非更新) を lock。
注: LOUD_FAIL_STATE は module 定数のため IMPROVE_DIR でなく LOUD_FAIL_STATE 自体を patch する
(cross-check reviewer N5 の指摘どおり)。
"""
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import clone_improve_lib as lib  # noqa: E402


@pytest.fixture()
def isolated(tmp_path, monkeypatch):
    """state を tmp に隔離し line_push を記録 mock に差し替え。"""
    monkeypatch.setattr(lib, "LOUD_FAIL_STATE", tmp_path / "loud_fail_state.json")
    sent: list[str] = []
    monkeypatch.setattr(lib, "line_push", lambda t, **kw: (sent.append(t), True)[1])
    return sent


def test_threshold_and_cooldown(isolated):
    sent = isolated
    assert lib.loud_fail("c", False, "x", threshold=3, cooldown_h=1) is False
    assert lib.loud_fail("c", False, "x", threshold=3, cooldown_h=1) is False
    assert lib.loud_fail("c", False, "x", threshold=3, cooldown_h=1) is True   # 3回目で発火
    assert len(sent) == 1 and "3 回連続" in sent[0] and "x" in sent[0]
    # cooldown 内は streak が伸びても再通知しない
    assert lib.loud_fail("c", False, "x", threshold=3, cooldown_h=1) is False
    assert len(sent) == 1


def test_success_resets_streak_but_keeps_cooldown(isolated):
    sent = isolated
    for _ in range(3):
        lib.loud_fail("c", False, "x", threshold=3, cooldown_h=1)
    assert len(sent) == 1
    assert lib.loud_fail("c", True) is False          # リセット
    for _ in range(3):
        r = lib.loud_fail("c", False, "y", threshold=3, cooldown_h=1)
    assert r is False and len(sent) == 1              # last_alert 保持 = cooldown 継続
    assert lib.loud_fail("c", False, "z", threshold=3, cooldown_h=0) is True   # cooldown 0 なら発火
    assert len(sent) == 2


def test_components_independent(isolated):
    sent = isolated
    assert lib.loud_fail("a", False, "", threshold=1, cooldown_h=0) is True
    assert lib.loud_fail("b", False, "", threshold=2, cooldown_h=0) is False   # b は独立に 1 回目
    assert lib.loud_fail("b", False, "", threshold=2, cooldown_h=0) is True
    assert len(sent) == 2


def test_corrupt_state_degrades_gracefully(isolated, tmp_path):
    lib.LOUD_FAIL_STATE.write_text("{{{{not json", encoding="utf-8")
    assert lib.loud_fail("c", False, "", threshold=1, cooldown_h=0) is True    # {} に degrade して続行


def test_push_failure_retries_next_run(isolated, monkeypatch):
    """通知が失敗 (False) の間は last_alert を進めず、次 run で再試行する。"""
    calls = []
    monkeypatch.setattr(lib, "line_push", lambda t, **kw: (calls.append(t), False)[1])
    assert lib.loud_fail("c", False, "", threshold=1, cooldown_h=24) is False  # 送達失敗
    assert lib.loud_fail("c", False, "", threshold=1, cooldown_h=24) is False  # 再試行される
    assert len(calls) == 2


def test_loud_fail_pushes_as_critical(tmp_path, monkeypatch):
    """★2026-07-10: loud_fail は critical=True で push する (LW fallback 許可の配線 pin)。"""
    monkeypatch.setattr(lib, "LOUD_FAIL_STATE", tmp_path / "loud_fail_state.json")
    kws: list[dict] = []
    monkeypatch.setattr(lib, "line_push", lambda t, **kw: (kws.append(kw), True)[1])
    for _ in range(3):
        lib.loud_fail("crit_pin", False, "boom", threshold=3, cooldown_h=1)
    assert kws and kws[-1].get("critical") is True
