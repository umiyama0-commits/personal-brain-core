"""
clone_voice.py — ElevenLabs TTS API wrapper for うみやま voice clone.

うみやまAI の応答テキストを 海山さんの声 (Pro Voice Clone) で音声化する薄い層。

★2026-05-21 作成 / ★2026-05-25 強化 (rate-limit handling + daily credit cap):
  - 429 (rate limit) / 402 (quota exhausted) を typed exception で分離
  - daily credit cap (default 20k) で暴走 DoS path を防ぐ
Creator plan で初期運用、利用量に応じて Pro へ upgrade 検討。

クレジット消費 (1文字=N credits):
  - eleven_multilingual_v2 (default): 2 credits/char、高品質、Pro Clone と相性◎
  - eleven_turbo_v2_5: 0.5 credits/char、低レイテンシ・低コスト、品質やや劣る

env 設定:
  ELEVENLABS_API_KEY=sk_...    (Profile → API Keys、Text-to-Speech 権限のみで OK)
  ELEVENLABS_VOICE_ID=...      (Voice Lab → ⋮ → 声のIDをコピー)
  CLONE_VOICE_DAILY_CAP_CREDITS=20000  (省略時 20k、launch 後の運用で調整)

使い方 (Python):
  from clone_voice import tts_bytes, ElevenLabsRateLimit, ElevenLabsQuotaExceeded
  try:
      audio = await tts_bytes("こんにちは", http_client)
  except ElevenLabsRateLimit:
      ...   # 429 → user に「混んでる、後で」
  except ElevenLabsQuotaExceeded:
      ...   # 402 / daily cap → admin に alert、TTS 機能 disable
"""
from __future__ import annotations

import logging
import os
from datetime import datetime
from typing import AsyncIterator, Optional
from zoneinfo import ZoneInfo

import httpx

logger = logging.getLogger(__name__)
JST = ZoneInfo("Asia/Tokyo")

ELEVENLABS_API_BASE = "https://api.elevenlabs.io"

# Model 識別子 (ElevenLabs 公式)
MODEL_MULTILINGUAL_V2 = "eleven_multilingual_v2"  # 高品質、2 credits/char
MODEL_TURBO_V2_5 = "eleven_turbo_v2_5"            # 低コスト、0.5 credits/char
DEFAULT_MODEL = MODEL_MULTILINGUAL_V2

# 出力フォーマット
#   mp3_22050_32   = 低品質、最小サイズ
#   mp3_44100_64   = 標準
#   mp3_44100_128  = 高音質 (default)
#   mp3_44100_192  = Pro plan のみ
DEFAULT_OUTPUT_FORMAT = "mp3_44100_128"


# ─── Typed exceptions (★2026-05-25 MUST-FIX M-1) ─────────────
class ElevenLabsError(Exception):
    """ElevenLabs API related error の base class。"""


class ElevenLabsRateLimit(ElevenLabsError):
    """429 Too Many Requests — 短時間に集中アクセスで API rate limit hit。
    一時的、数秒〜数分で復活する想定。"""


class ElevenLabsQuotaExceeded(ElevenLabsError):
    """402 Payment Required / quota_exceeded — 月次 credit / plan limit 到達。
    月初まで復活しない、admin 介入 (upgrade or 静観) 必要。"""


# ─── Daily credit cap (★2026-05-25 MUST-FIX M-3) ─────────────
# in-memory counter (per process、container restart で reset)。
# launch 初日に script loop で /api/clone-voice/test を叩かれて
# Creator plan 121k credits を分単位で焼かれる DoS path を防ぐ。
# 厳格な永続化 cap が必要なら redis や DB に移すが、まず in-memory で十分。
_DAILY_CAP_CREDITS = int(os.getenv("CLONE_VOICE_DAILY_CAP_CREDITS", "20000"))
_credit_usage = {"date": None, "total": 0}


def get_daily_usage() -> dict:
    """現在の daily credit 使用状況を返す (monitoring 用)。"""
    return dict(_credit_usage)


def _check_and_record_credits(estimated: int) -> None:
    """daily cap check + 加算。超過なら ElevenLabsQuotaExceeded raise。"""
    today = datetime.now(JST).date()
    if _credit_usage["date"] != today:
        _credit_usage["date"] = today
        _credit_usage["total"] = 0
    projected = _credit_usage["total"] + estimated
    if projected > _DAILY_CAP_CREDITS:
        logger.error(
            f"🚨 [clone-voice] DAILY CAP HIT: {_credit_usage['total']:,} + "
            f"{estimated:,} = {projected:,} > {_DAILY_CAP_CREDITS:,} (req REJECTED)"
        )
        raise ElevenLabsQuotaExceeded(
            f"Daily cap reached: {_credit_usage['total']:,}/{_DAILY_CAP_CREDITS:,} credits today, "
            f"requested {estimated:,} more would exceed. Resets at JST midnight."
        )
    _credit_usage["total"] = projected
    if projected > _DAILY_CAP_CREDITS * 0.8:
        logger.warning(
            f"⚠️ [clone-voice] daily usage >80%: {projected:,}/{_DAILY_CAP_CREDITS:,} "
            f"({100 * projected / _DAILY_CAP_CREDITS:.1f}%)"
        )


def is_configured() -> bool:
    """ELEVENLABS_API_KEY と ELEVENLABS_VOICE_ID が両方 set か。"""
    return bool(os.getenv("ELEVENLABS_API_KEY", "") and os.getenv("ELEVENLABS_VOICE_ID", ""))


def estimate_credits(text: str, model: str = DEFAULT_MODEL) -> int:
    """テキストから credit 消費量を推定 (decision support 用)。"""
    n = len(text)
    if model == MODEL_TURBO_V2_5:
        return int(n * 0.5)
    return n * 2  # multilingual_v2 / 他は概ね 2 credits/char


def _classify_api_error(resp: httpx.Response) -> Exception:
    """ElevenLabs response から適切な exception を作る (★2026-05-25 M-1)。"""
    sc = resp.status_code
    body = resp.text[:300]
    if sc == 429:
        logger.error(f"🚨 [clone-voice] 429 RATE LIMIT: {body}")
        return ElevenLabsRateLimit(f"ElevenLabs 429 rate limit: {body}")
    # 402 Payment Required = quota exhausted (Creator 121k 等を使い切り)
    # body に "quota_exceeded" / "credit" / "exceeded" 等が含まれる場合も
    if sc == 402 or "quota_exceeded" in body.lower() or "credit" in body.lower() and "exceed" in body.lower():
        logger.error(f"🚨 [clone-voice] QUOTA EXHAUSTED ({sc}): {body}")
        return ElevenLabsQuotaExceeded(f"ElevenLabs quota exhausted ({sc}): {body}")
    # その他は素の HTTPStatusError
    return httpx.HTTPStatusError(
        f"ElevenLabs API {sc}: {body}", request=resp.request, response=resp
    )


async def tts_bytes(
    text: str,
    http: httpx.AsyncClient,
    voice_id: Optional[str] = None,
    model: str = DEFAULT_MODEL,
    output_format: str = DEFAULT_OUTPUT_FORMAT,
    stability: float = 0.5,
    similarity_boost: float = 0.75,
    style: float = 0.0,
    use_speaker_boost: bool = True,
    timeout: float = 60.0,
) -> bytes:
    """テキスト → mp3 bytes (non-streaming)。短文 / 単発再生向け。

    voice_settings:
      stability: 0.0-1.0  低いほど表現豊か、高いほど安定 (default 0.5)
      similarity_boost: 0.0-1.0  訓練元音声への忠実度 (default 0.75)
      style: 0.0-1.0  抑揚の強さ。Pro Clone なら 0 でも十分声色は乗る
      use_speaker_boost: True で話者特徴を強調

    Raises:
      RuntimeError — env 未設定
      ElevenLabsRateLimit — 429 rate limit (短時間 retry 可)
      ElevenLabsQuotaExceeded — 402 quota / daily cap 到達 (admin 介入要)
      httpx.HTTPStatusError — その他 API エラー
    """
    vid = voice_id or os.getenv("ELEVENLABS_VOICE_ID", "")
    key = os.getenv("ELEVENLABS_API_KEY", "")
    if not vid or not key:
        raise RuntimeError(
            "ELEVENLABS_API_KEY / ELEVENLABS_VOICE_ID 未設定 (.env に追加して line-bot 再起動)"
        )

    # ★pre-flight: daily cap check (M-3)
    estimated = estimate_credits(text, model)
    _check_and_record_credits(estimated)

    url = f"{ELEVENLABS_API_BASE}/v1/text-to-speech/{vid}"
    headers = {
        "xi-api-key": key,
        "Content-Type": "application/json",
        "Accept": "audio/mpeg",
    }
    params = {"output_format": output_format}
    body = {
        "text": text,
        "model_id": model,
        "voice_settings": {
            "stability": stability,
            "similarity_boost": similarity_boost,
            "style": style,
            "use_speaker_boost": use_speaker_boost,
        },
    }
    logger.info(
        f"[clone-voice] TTS request: {len(text)} chars, model={model}, "
        f"format={output_format}, est_credits={estimated} "
        f"(daily {_credit_usage['total']:,}/{_DAILY_CAP_CREDITS:,})"
    )
    resp = await http.post(url, headers=headers, params=params, json=body, timeout=timeout)
    if resp.status_code != 200:
        raise _classify_api_error(resp)
    audio = resp.content
    logger.info(f"[clone-voice] TTS OK: {len(audio):,} bytes ({len(audio)/1024:.1f}KB)")
    return audio


async def tts_stream(
    text: str,
    http: httpx.AsyncClient,
    voice_id: Optional[str] = None,
    model: str = DEFAULT_MODEL,
    output_format: str = DEFAULT_OUTPUT_FORMAT,
    stability: float = 0.5,
    similarity_boost: float = 0.75,
    style: float = 0.0,
    use_speaker_boost: bool = True,
    timeout: float = 120.0,
) -> AsyncIterator[bytes]:
    """テキスト → mp3 chunks (streaming)。長文 / 低レイテンシ用途向け。

    利用: async for chunk in tts_stream(...): yield chunk

    Raises:
      RuntimeError — env 未設定
      ElevenLabsRateLimit / ElevenLabsQuotaExceeded — pre-flight or API レベル
      httpx.HTTPStatusError — その他
    """
    vid = voice_id or os.getenv("ELEVENLABS_VOICE_ID", "")
    key = os.getenv("ELEVENLABS_API_KEY", "")
    if not vid or not key:
        raise RuntimeError(
            "ELEVENLABS_API_KEY / ELEVENLABS_VOICE_ID 未設定 (.env に追加して line-bot 再起動)"
        )

    estimated = estimate_credits(text, model)
    _check_and_record_credits(estimated)

    url = f"{ELEVENLABS_API_BASE}/v1/text-to-speech/{vid}/stream"
    headers = {
        "xi-api-key": key,
        "Content-Type": "application/json",
        "Accept": "audio/mpeg",
    }
    params = {"output_format": output_format}
    body = {
        "text": text,
        "model_id": model,
        "voice_settings": {
            "stability": stability,
            "similarity_boost": similarity_boost,
            "style": style,
            "use_speaker_boost": use_speaker_boost,
        },
    }
    logger.info(
        f"[clone-voice] TTS stream: {len(text)} chars, model={model}, "
        f"est_credits={estimated} (daily {_credit_usage['total']:,}/{_DAILY_CAP_CREDITS:,})"
    )
    async with http.stream(
        "POST", url, headers=headers, params=params, json=body, timeout=timeout
    ) as resp:
        if resp.status_code != 200:
            # stream モードでは body を別途読む必要
            err_bytes = await resp.aread()
            # 簡易 fake response で _classify_api_error に流す
            class _FakeResp:
                status_code = resp.status_code
                text = err_bytes.decode("utf-8", errors="replace")[:300]
                request = resp.request
                response = resp
            raise _classify_api_error(_FakeResp())
        async for chunk in resp.aiter_bytes():
            if chunk:
                yield chunk
