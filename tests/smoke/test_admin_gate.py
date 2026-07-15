"""smoke test: services/auth.py + main.py admin gate (★2026-05-23 LEE §3.2)。

LINE Bot で第三者が /claude 等の管理コマンドを送っても拒否される事を保証する。
fail-closed: 環境変数未設定なら全拒否。
"""
from __future__ import annotations

import importlib
from pathlib import Path

import pytest


REPO = Path(__file__).resolve().parent.parent.parent


# ─── is_admin / is_lw_admin の単体 ─────────────
@pytest.mark.smoke
def test_is_admin_matches_env_id(monkeypatch):
    monkeypatch.setenv("ALIGNMENT_TARGET_USER", "U_admin_xxx")
    import services.auth as auth
    importlib.reload(auth)
    assert auth.is_admin("U_admin_xxx") is True
    assert auth.is_admin("U_other_yyy") is False
    assert auth.is_admin("") is False


@pytest.mark.smoke
def test_is_admin_fail_closed_when_env_unset(monkeypatch):
    """env が空なら全拒否 (= 全部 False、安全側 fail-closed)。"""
    monkeypatch.delenv("ALIGNMENT_TARGET_USER", raising=False)
    import services.auth as auth
    importlib.reload(auth)
    assert auth.is_admin("U_admin_xxx") is False
    assert auth.is_admin("U_anything") is False
    assert auth.is_admin("") is False


@pytest.mark.smoke
def test_is_lw_admin_matches_env_id(monkeypatch):
    monkeypatch.setenv("ADMIN_LW_USER_ID", "lw_admin_id")
    import services.auth as auth
    importlib.reload(auth)
    assert auth.is_lw_admin("lw_admin_id") is True
    assert auth.is_lw_admin("lw_staff_xxx") is False


@pytest.mark.smoke
def test_is_lw_admin_fail_closed_when_env_unset(monkeypatch):
    monkeypatch.delenv("ADMIN_LW_USER_ID", raising=False)
    import services.auth as auth
    importlib.reload(auth)
    assert auth.is_lw_admin("anyone") is False


@pytest.mark.smoke
def test_reject_message_is_admin_only():
    """非管理者に返す統一文言が「管理者専用」を明示している。"""
    import services.auth as auth
    msg = auth.reject_message()
    assert "管理者" in msg


# ─── main.py 内に admin gate が埋め込まれている事 (= deploy 漏れ防止) ─────────────
@pytest.mark.smoke
def test_main_py_imports_auth_helpers():
    src = (REPO / "main.py").read_text(encoding="utf-8")
    assert "from services.auth import is_admin" in src
    assert "reject_message" in src


@pytest.mark.smoke
def test_main_py_gates_claude_command():
    """/claude 分岐に admin gate が入っている。"""
    src = (REPO / "main.py").read_text(encoding="utf-8")
    # /claude の分岐直後に is_admin チェックがある
    # (= startswith("/claude") の数行以内に is_admin(user_id) が出現する)
    idx = src.find('startswith("/claude ") or user_message.strip() == "/claude"')
    assert idx > 0, "/claude 分岐が見つからない"
    window = src[idx:idx + 600]
    assert "is_admin(user_id)" in window, "/claude 分岐に admin gate 無し"


@pytest.mark.smoke
def test_main_py_gates_brain_commands():
    """/brain /teach /memo /clone /lint 等の分岐に admin gate がある。"""
    src = (REPO / "main.py").read_text(encoding="utf-8")
    idx = src.find('startswith(("/brain", "/teach", "/memo", "/clone"')
    assert idx > 0
    window = src[idx:idx + 600]
    assert "is_admin(user_id)" in window


@pytest.mark.smoke
def test_main_py_gates_privacy_commands():
    """/filter /block /unblock /quarantine の分岐に admin gate がある。"""
    src = (REPO / "main.py").read_text(encoding="utf-8")
    idx = src.find('startswith(("/filter", "/block"')
    assert idx > 0
    window = src[idx:idx + 600]
    assert "is_admin(user_id)" in window


@pytest.mark.smoke
def test_main_py_gates_reset_command():
    """/reset 分岐に admin gate がある (会話履歴削除は破壊的)。"""
    src = (REPO / "main.py").read_text(encoding="utf-8")
    idx = src.find('user_message.strip() == "/reset"')
    assert idx > 0
    window = src[idx:idx + 600]
    assert "is_admin(user_id)" in window
