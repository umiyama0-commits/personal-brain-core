"""smoke test: /video-align Brain bridge (★2026-05-27 海山指示)

既存 /video-align scaffold (= 2e8fc85 海山実装、Day 1-2、textarea → avatar speak echo) に
**Brain bridge** を 拡張追加: user query → clone_respond_public → avatar speak.

surgical edit:
- _VIDEO_ALIGN_HTML に chat section UI 追加 (= 既存 echo モード維持)
- 新 endpoint POST /api/video-alignment/respond で Brain bridge
- env: VIDEO_ALIGN_MODEL (= 任意、default は CLONE_PUBLIC_PROD_MODEL fallback)
"""
from __future__ import annotations

from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent.parent


# ─── HTML 拡張 ─────
@pytest.mark.smoke
def test_video_align_html_has_chat_section():
    """_VIDEO_ALIGN_HTML に Brain 対話 chat section 追加."""
    src = (REPO / "main.py").read_text(encoding="utf-8")
    # _VIDEO_ALIGN_HTML 内
    idx = src.find("_VIDEO_ALIGN_HTML")
    assert idx > 0
    body = src[idx : idx + 10000]
    # chat UI elements
    assert 'id="chat-log"' in body
    assert 'id="chat-input"' in body
    assert 'id="btn-chat-send"' in body
    assert 'id="btn-chat-clear"' in body
    # Brain 対話 label
    assert "Brain 対話" in body or "壁打ち" in body
    # 既存 echo モードは維持
    assert 'id="text-input"' in body
    assert 'id="btn-speak"' in body
    # RESPOND_URL placeholder
    assert "__RESPOND_URL__" in body


# ─── JS bridge logic ─────
@pytest.mark.smoke
def test_video_align_html_has_brain_bridge_js():
    """chat send → fetch RESPOND_URL → avatar.speak (= Brain bridge JS)."""
    src = (REPO / "main.py").read_text(encoding="utf-8")
    idx = src.find("_VIDEO_ALIGN_HTML")
    body = src[idx : idx + 10000]
    # chatSend function
    assert "function chatSend" in body or "async function chatSend" in body
    # fetch RESPOND_URL
    assert "RESPOND_URL" in body
    assert "POST" in body
    # avatar.speak with reply
    assert "avatar.speak" in body
    assert 'taskType: "REPEAT"' in body
    # chat history 管理
    assert "chatHistory" in body


# ─── respond endpoint ─────
@pytest.mark.smoke
def test_video_align_respond_endpoint_exists():
    """POST /api/video-alignment/respond endpoint (= Brain bridge)."""
    src = (REPO / "main.py").read_text(encoding="utf-8")
    assert '@app.post("/api/video-alignment/respond")' in src
    assert "async def video_align_respond" in src
    idx = src.find("async def video_align_respond")
    assert idx > 0
    body = src[idx : idx + 2500]
    # 既存 _safe_clone_respond 経由 (= wiki + style + memory 既存)
    assert "_safe_clone_respond" in body
    # model 選択可能 (= VIDEO_ALIGN_MODEL env)
    assert "VIDEO_ALIGN_MODEL" in body
    # token 認証 (= VOICE_ALIGN_TOKEN 流用)
    assert "VOICE_ALIGN_TOKEN" in body
    # reply 返却
    assert '"reply": reply' in body or "reply" in body


@pytest.mark.smoke
def test_video_align_respond_request_model():
    """VideoAlignRespondRequest BaseModel (= query + history)."""
    src = (REPO / "main.py").read_text(encoding="utf-8")
    assert "class VideoAlignRespondRequest(BaseModel):" in src
    idx = src.find("class VideoAlignRespondRequest")
    body = src[idx : idx + 500]
    assert "query: str" in body
    assert "history:" in body


# ─── placeholder replace ─────
@pytest.mark.smoke
def test_video_align_page_replaces_respond_url_placeholder():
    """video_align_page で __RESPOND_URL__ を実 URL に replace."""
    src = (REPO / "main.py").read_text(encoding="utf-8")
    idx = src.find("async def video_align_page")
    assert idx > 0
    end_idx = src.find("\nasync def ", idx + 1)
    body = src[idx : end_idx if end_idx > 0 else idx + 3000]
    assert "__RESPOND_URL__" in body
    assert "/api/video-alignment/respond" in body
