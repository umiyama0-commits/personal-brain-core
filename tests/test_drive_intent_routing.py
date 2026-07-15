"""tests/test_drive_intent_routing.py — Drive 検索 intent 明示時のみ route

★2026-05-26 海山指示「通常会話で proactive Drive 表示は禁止、明示時のみ実行」:
- _has_drive_intent: "Drive" / "ドライブ" を含む明示的 query のみ True
- 通常会話は False、clone_respond のみ通る
- 自動 follow-up button は削除済 (= _maybe_offer_drive_search の呼出無し確認)
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

# fastapi 必要 (= main.py import)、MacBook では skip
try:
    import fastapi  # noqa: F401
    _HAS_FASTAPI = True
except ImportError:
    _HAS_FASTAPI = False
needs_fastapi = pytest.mark.skipif(not _HAS_FASTAPI, reason="fastapi 未 install (= local MacBook)")


# ─── _has_drive_intent: 明示パターン検出 ─────────────
@needs_fastapi
def test_drive_intent_explicit_keywords_detected():
    import main
    # Drive 明示
    assert main._has_drive_intent("Drive で武蔵小山の予算 探して")
    assert main._has_drive_intent("Driveを検索")
    assert main._has_drive_intent("Drive 内に去年の議事録ある?")
    assert main._has_drive_intent("Drive から探して")
    # ドライブ (カタカナ)
    assert main._has_drive_intent("ドライブで Monday Dash 探")
    assert main._has_drive_intent("ドライブ内の資料教えて")
    assert main._has_drive_intent("ドライブから取ってきて")
    # Google Drive
    assert main._has_drive_intent("Google Drive 検索して")
    assert main._has_drive_intent("Google ドライブにある?")


@needs_fastapi
def test_drive_intent_not_triggered_for_normal_conversation():
    """通常会話 (= Drive keyword 無し) では intent 検出されない"""
    import main
    # 通常質問
    assert not main._has_drive_intent("武蔵小山の今月予算は?")
    assert not main._has_drive_intent("先週の Monday Dash の内容")
    assert not main._has_drive_intent("今日の客数は?")
    # ambiguous (= 海山指示で 「探す」「ファイル」 単独は除外)
    assert not main._has_drive_intent("資料を探して")  # ★「資料」「探す」 単独は trigger しない
    assert not main._has_drive_intent("ファイルどこ?")
    assert not main._has_drive_intent("教えて")
    # empty / None
    assert not main._has_drive_intent("")
    assert not main._has_drive_intent(None)


@needs_fastapi
def test_drive_intent_case_insensitive():
    """大文字小文字違いでも検出"""
    import main
    assert main._has_drive_intent("DRIVE で探して")
    assert main._has_drive_intent("drive で探して")
    assert main._has_drive_intent("Drive で探して")
    assert main._has_drive_intent("DriveDe探して")  # = Drive de → drive de 含む


@needs_fastapi
def test_main_does_not_call_offer_drive_search_anymore():
    """★削除確認: _maybe_offer_drive_search 自動呼出を main.py 内 reply path から削除"""
    src = (REPO_ROOT / "main.py").read_text(encoding="utf-8")
    # 関数 definition は残ってる (= 削除しない、deprecated function)
    # OR 完全削除 — 今回は完全削除した
    # 呼出箇所が 0 件であること
    # `asyncio.create_task(\n            _maybe_offer_drive_search` 形式の呼出は無い
    assert "asyncio.create_task(\n            _maybe_offer_drive_search" not in src
    assert "asyncio.create_task(_maybe_offer_drive_search" not in src


@needs_fastapi
def test_drive_intent_handler_exists():
    """_handle_drive_intent_query 関数が main に存在"""
    import main
    assert hasattr(main, "_handle_drive_intent_query")
    assert callable(main._handle_drive_intent_query)


@needs_fastapi
def test_has_drive_intent_function_exists():
    """_has_drive_intent 関数が main に存在 + callable"""
    import main
    assert hasattr(main, "_has_drive_intent")
    assert callable(main._has_drive_intent)


def test_main_py_source_includes_intent_route_in_dm_path():
    """main.py source に DM path での intent check が含まれる"""
    src = (REPO_ROOT / "main.py").read_text(encoding="utf-8")
    # _has_drive_intent(text) が DM path にある
    assert "_has_drive_intent(text)" in src
    # _has_drive_intent(clean_text) が group path にある
    assert "_has_drive_intent(clean_text)" in src
    # _handle_drive_intent_query 呼出
    assert "_handle_drive_intent_query(" in src
