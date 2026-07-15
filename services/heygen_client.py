"""services/heygen_client.py — HeyGen Streaming Avatar API client (雛型).

★2026-05-26 海山指示「本人のモーションアバターを作りたい」 (use case = Live video 通話、
photorealistic 画質、HeyGen 1 週間 trial) を受けた scaffolding。

HeyGen Streaming Avatar (= Interactive Avatar) は WebRTC で real-time に
photorealistic avatar 動画を delivery する API。本 module は server-side client、
browser 側は別途 HeyGen JS SDK or raw WebRTC で session に join する。

# 主要 API endpoint (= 2026-05 時点、https://docs.heygen.com/reference)
1. POST /v1/streaming.create_token     → 短期 access_token (= browser に渡す)
2. POST /v1/streaming.new              → session_id + WebRTC offer SDP
3. POST /v1/streaming.start            → streaming 開始
4. POST /v1/streaming.task             → text を avatar に喋らせる
5. POST /v1/streaming.stop             → session close
6. GET  /v1/streaming.list             → active session 一覧
7. GET  /v1/avatar.list                → 自分の avatar 一覧 (= avatar_id 取得用)

# 主要 env vars
  HEYGEN_API_KEY      — HeyGen dashboard → Account → API Token から取得
  HEYGEN_AVATAR_ID    — 海山さん本人 Custom Avatar (Pro Avatar / Avatar IV)
                        の voice ID。Day 3 (= avatar 生成完了) 後に投入。
                        空のままなら demo avatar (Anna_public_3_20240108) を使用。
  HEYGEN_VOICE_ID     — TTS voice ID。空なら HeyGen default、海山 Voice Clone
                        を HeyGen 側で連携できれば ElevenLabs と統一可能。
                        (= 検証未済、Day 4-5 で確認)

# 雛型としての注意
本 module は **Day 1-2 並行 scaffolding**。実際に動くかは Day 3 以降の
testing で確認。雛型段階で 想定されてない error / spec drift 出れば
HeyGen 公式 docs (https://docs.heygen.com/) と再照合して修正する。
"""
from __future__ import annotations

import asyncio
import logging
import os
from typing import Optional

import httpx

logger = logging.getLogger(__name__)

HEYGEN_API_KEY = os.getenv("HEYGEN_API_KEY", "")
HEYGEN_AVATAR_ID = os.getenv("HEYGEN_AVATAR_ID", "")
HEYGEN_VOICE_ID = os.getenv("HEYGEN_VOICE_ID", "")
HEYGEN_BASE = "https://api.heygen.com"

# Day 3 までは公開 demo avatar で pipeline 動作 verify (= Anna は HeyGen の
# 標準テスト avatar、API key あれば誰でも使える)
DEMO_AVATAR_ID = "Anna_public_3_20240108"

HEYGEN_TIMEOUT_SEC = 30


class HeyGenUnavailableError(Exception):
    """API key 未設定 / API 失敗 / quota 枯渇 で raise."""


def _ensure_api_key() -> None:
    if not HEYGEN_API_KEY:
        raise HeyGenUnavailableError(
            "HEYGEN_API_KEY 未設定。https://app.heygen.com → Account → API Token で取得し .env へ"
        )


def _resolve_avatar_id() -> str:
    """環境変数優先、未設定なら DEMO_AVATAR_ID。"""
    if HEYGEN_AVATAR_ID:
        return HEYGEN_AVATAR_ID
    logger.warning(
        "[heygen] HEYGEN_AVATAR_ID 未設定、demo avatar (Anna_public_3) で pipeline verify。"
        " Day 3 で 海山 Custom Avatar 完了後に .env 更新が必要。"
    )
    return DEMO_AVATAR_ID


async def _post(path: str, body: dict, timeout: int = HEYGEN_TIMEOUT_SEC) -> dict:
    """HeyGen API への POST helper。失敗時 HeyGenUnavailableError raise."""
    _ensure_api_key()
    url = f"{HEYGEN_BASE}{path}"
    headers = {
        "X-Api-Key": HEYGEN_API_KEY,
        "Content-Type": "application/json",
        "Accept": "application/json",
    }
    async with httpx.AsyncClient(timeout=timeout) as client:
        try:
            r = await client.post(url, json=body, headers=headers)
        except Exception as e:
            raise HeyGenUnavailableError(f"network error: {type(e).__name__}: {e}")
    if r.status_code >= 400:
        raise HeyGenUnavailableError(
            f"API {path} status {r.status_code}: {r.text[:300]}"
        )
    try:
        return r.json()
    except Exception as e:
        raise HeyGenUnavailableError(f"JSON parse error: {e}; body[:200]={r.text[:200]}")


async def _get(path: str, timeout: int = HEYGEN_TIMEOUT_SEC) -> dict:
    """HeyGen API への GET helper。"""
    _ensure_api_key()
    url = f"{HEYGEN_BASE}{path}"
    headers = {
        "X-Api-Key": HEYGEN_API_KEY,
        "Accept": "application/json",
    }
    async with httpx.AsyncClient(timeout=timeout) as client:
        try:
            r = await client.get(url, headers=headers)
        except Exception as e:
            raise HeyGenUnavailableError(f"network error: {type(e).__name__}: {e}")
    if r.status_code >= 400:
        raise HeyGenUnavailableError(f"API {path} status {r.status_code}: {r.text[:300]}")
    try:
        return r.json()
    except Exception as e:
        raise HeyGenUnavailableError(f"JSON parse error: {e}; body[:200]={r.text[:200]}")


# ─── Streaming Avatar API ─────────────────────────────────


async def create_streaming_token() -> str:
    """短期 streaming token を発行 (= browser 側で使用、API key を直接渡さない)。"""
    data = await _post("/v1/streaming.create_token", {})
    token = (data.get("data") or {}).get("token") or data.get("token") or ""
    if not token:
        raise HeyGenUnavailableError(f"token not in response: {data}")
    return token


async def create_streaming_session(
    avatar_id: Optional[str] = None,
    voice_id: Optional[str] = None,
    quality: str = "high",  # "low" / "medium" / "high" / "ultra"
) -> dict:
    """新 streaming session を作成。session_id と WebRTC offer SDP を返す。

    Returns:
        {"session_id": str, "sdp": dict (offer), "ice_servers": list, ...}
    """
    body = {
        "avatar_id": avatar_id or _resolve_avatar_id(),
        "quality": quality,
        "version": "v2",  # 新 API version (= v1 deprecated)
    }
    if voice_id or HEYGEN_VOICE_ID:
        body["voice"] = {"voice_id": voice_id or HEYGEN_VOICE_ID}
    data = await _post("/v1/streaming.new", body)
    inner = data.get("data") or data
    return inner


async def start_streaming_session(session_id: str, sdp_answer: dict) -> dict:
    """WebRTC handshake 完了 → streaming 開始。

    Args:
        session_id: create_streaming_session の戻り値
        sdp_answer: browser 側の WebRTC SDP answer
    """
    body = {"session_id": session_id, "sdp": sdp_answer}
    return await _post("/v1/streaming.start", body)


async def send_text_to_avatar(
    session_id: str,
    text: str,
    task_type: str = "talk",  # "talk" / "repeat"
) -> dict:
    """avatar に喋らせる text を送信。

    task_type:
      - "talk": HeyGen の LLM で考えさせて喋らせる (= 単独 chat)
      - "repeat": 渡した text を そのまま読み上げる (= bot が外で生成した text 用、★こっち推奨)

    voice-align integration 想定: brain-agent の clone_respond_public 出力を
    repeat task で avatar に渡す → 海山 voice + 海山 face で出力。
    """
    if not text:
        return {}
    body = {
        "session_id": session_id,
        "text": text[:1900],  # LineWorks と同じ 1900 字制限、超過は事前に chunk 推奨
        "task_type": task_type,
    }
    return await _post("/v1/streaming.task", body)


async def stop_streaming_session(session_id: str) -> dict:
    """session を close (= 課金 stop)。clean shutdown 必須。"""
    body = {"session_id": session_id}
    try:
        return await _post("/v1/streaming.stop", body)
    except HeyGenUnavailableError as e:
        # close 失敗は warning だけ (= server-side 側で auto-cleanup される)
        logger.warning(f"[heygen] stop session {session_id} failed: {e}")
        return {}


async def list_streaming_sessions() -> list[dict]:
    """active session 一覧 (= leaked session の cleanup 用)。"""
    data = await _get("/v1/streaming.list")
    return (data.get("data") or {}).get("sessions") or []


async def list_avatars() -> list[dict]:
    """自分のアカウントで使用可能な avatar 一覧 (= 海山 Custom Avatar 確認用)。"""
    data = await _get("/v1/avatar.list")
    return (data.get("data") or {}).get("avatars") or []


# ─── Convenience helpers ──────────────────────────────────


async def cleanup_leaked_sessions(max_age_min: int = 10) -> int:
    """古い leaked session を強制 close (= 不正な切断 / browser crash で残った session)。

    Returns: 閉じた session の数
    """
    try:
        sessions = await list_streaming_sessions()
    except HeyGenUnavailableError as e:
        logger.warning(f"[heygen] cleanup list failed: {e}")
        return 0
    closed = 0
    for s in sessions:
        sid = s.get("session_id") or s.get("id")
        if not sid:
            continue
        try:
            await stop_streaming_session(sid)
            closed += 1
            logger.info(f"[heygen] cleaned leaked session {sid}")
        except Exception:
            pass
    return closed


async def healthcheck() -> dict:
    """簡易動作確認 (= deploy 時 + health endpoint で使用)。

    Returns: {"ok": bool, "api_key_set": bool, "avatar_id": str, "avatars_n": int, ...}
    """
    result: dict = {
        "api_key_set": bool(HEYGEN_API_KEY),
        "avatar_id": _resolve_avatar_id(),
        "using_demo": not bool(HEYGEN_AVATAR_ID),
    }
    if not HEYGEN_API_KEY:
        result["ok"] = False
        result["error"] = "HEYGEN_API_KEY 未設定"
        return result
    try:
        avatars = await list_avatars()
        result["avatars_n"] = len(avatars)
        result["ok"] = True
    except HeyGenUnavailableError as e:
        result["ok"] = False
        result["error"] = str(e)[:200]
    return result
