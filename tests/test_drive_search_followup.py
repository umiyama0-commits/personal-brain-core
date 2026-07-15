"""tests/test_drive_search_followup.py — 「データ無い」回答時の Drive 検索 button follow-up

★2026-05-26 海山指示「Drive 内で検索しますか?」 button を追加。
data_gap_detector で hit 時、LINE Works に button_template (= 1-tap) を follow-up。
"""
from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))


# fastapi 必要 (= main.py import)。MacBook では skip、Mac Studio で通る。
try:
    import fastapi  # noqa: F401
    _HAS_FASTAPI = True
except ImportError:
    _HAS_FASTAPI = False
needs_fastapi = pytest.mark.skipif(not _HAS_FASTAPI, reason="fastapi 未 install (= local MacBook)")


@needs_fastapi
@pytest.mark.asyncio
async def test_maybe_offer_drive_search_no_gap():
    """「データ無い」 keyword 含まない reply → button 送信なし"""
    import main
    http = MagicMock()
    send_button = AsyncMock()
    monkey_lw = MagicMock()
    monkey_lw.send_button_template = send_button
    monkey_lw.send_channel_text = AsyncMock()
    main.lineworks_bot = monkey_lw

    await main._maybe_offer_drive_search(
        http, "user_xyz", "全社売上は?", "100M 円、予算比 95%",
        via_channel=False,
    )
    send_button.assert_not_awaited()
    monkey_lw.send_channel_text.assert_not_awaited()


@needs_fastapi
@pytest.mark.asyncio
async def test_maybe_offer_drive_search_dm_sends_button():
    """「データ無い」 含む reply (DM) → send_button_template が呼ばれる"""
    import main
    http = MagicMock()
    send_button = AsyncMock()
    monkey_lw = MagicMock()
    monkey_lw.send_button_template = send_button
    main.lineworks_bot = monkey_lw

    await main._maybe_offer_drive_search(
        http, "user_xyz",
        "武蔵小山の去年の客単価は?",
        "そこのデータがないんだよね、まだ流し込めてない",
        via_channel=False,
    )
    send_button.assert_awaited_once()
    call_args = send_button.call_args
    # 引数: (http, user_id, button_text, [{"label": ..., "postback": "/drive ai ..."}])
    assert call_args.args[1] == "user_xyz"
    button_text = call_args.args[2]
    assert "Drive" in button_text
    buttons = call_args.args[3]
    assert len(buttons) == 1
    assert buttons[0]["postback"].startswith("/drive ai ")
    assert "武蔵小山" in buttons[0]["postback"]


@needs_fastapi
@pytest.mark.asyncio
async def test_maybe_offer_drive_search_channel_sends_text():
    """group chat (= via_channel=True) では button_template 不可 → text のみ"""
    import main
    http = MagicMock()
    send_channel = AsyncMock()
    monkey_lw = MagicMock()
    monkey_lw.send_channel_text = send_channel
    monkey_lw.send_button_template = AsyncMock()
    main.lineworks_bot = monkey_lw

    await main._maybe_offer_drive_search(
        http, "user_xyz",
        "去年の予算は?",
        "データ無いです",
        via_channel=True,
        channel_id="ch_001",
    )
    send_channel.assert_awaited_once()
    text = send_channel.call_args.args[2]
    assert "/drive ai" in text
    assert "去年の予算は?" in text
    # button_template は呼ばれない (= group では使えない)
    monkey_lw.send_button_template.assert_not_awaited()


@needs_fastapi
@pytest.mark.asyncio
async def test_maybe_offer_drive_search_query_truncated():
    """長い query は 200 字 cap"""
    import main
    http = MagicMock()
    send_button = AsyncMock()
    monkey_lw = MagicMock()
    monkey_lw.send_button_template = send_button
    main.lineworks_bot = monkey_lw

    long_query = "あ" * 500
    await main._maybe_offer_drive_search(
        http, "u", long_query, "そんなデータ無いね", via_channel=False,
    )
    send_button.assert_awaited_once()
    payload = send_button.call_args.args[3][0]["postback"]
    # /drive ai prefix を除いた部分が 200 字以下
    assert len(payload[len("/drive ai "):]) <= 200


@needs_fastapi
@pytest.mark.asyncio
async def test_maybe_offer_drive_search_send_failure_silent():
    """send_button_template 失敗時に上位に伝播しない (= main reply に影響無し)"""
    import main
    http = MagicMock()
    send_button = AsyncMock(side_effect=Exception("network error"))
    monkey_lw = MagicMock()
    monkey_lw.send_button_template = send_button
    main.lineworks_bot = monkey_lw

    # 例外は内部 catch、上位に上がらない
    await main._maybe_offer_drive_search(
        http, "u", "去年は?", "データ無いね", via_channel=False,
    )
    send_button.assert_awaited_once()


@needs_fastapi
@pytest.mark.asyncio
async def test_maybe_offer_drive_search_detector_unavailable():
    """data_gap_detector import 失敗 → silent return"""
    import main
    http = MagicMock()
    send_button = AsyncMock()
    monkey_lw = MagicMock()
    monkey_lw.send_button_template = send_button
    main.lineworks_bot = monkey_lw

    # detect_data_gap を破壊
    import scripts.data_gap_detector as dgd
    orig = dgd.detect_data_gap

    def boom(*args, **kwargs):
        raise RuntimeError("detector broken")
    dgd.detect_data_gap = boom
    try:
        await main._maybe_offer_drive_search(http, "u", "x", "データ無いです", via_channel=False)
        send_button.assert_not_awaited()  # detector 死亡 → 何もしない
    finally:
        dgd.detect_data_gap = orig
