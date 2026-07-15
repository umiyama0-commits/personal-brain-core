"""tests/smoke/test_voice_recording_auth.py — Vapi 録音 DL の認証移行 pin (★2026-07-14).

Vapi breaking change (7/15〜 recordingUrl 非認証 fetch 不可、7/25〜 公開 URL 完全停止):
- VAPI_API_KEY + call_id があれば認証 endpoint (mono→stereo) を優先
- 認証不可なら legacy URL fallback (移行期/BYOK)
- 失敗は loud_fail (§1.18 — 一級の声データの silent 消失を通知)
source pin (main.py は fastapi 依存で import 不可のため)。
"""
from __future__ import annotations

from pathlib import Path

_ROOT = Path(__file__).resolve().parents[2]


def _fn_body() -> str:
    src = (_ROOT / "main.py").read_text(encoding="utf-8")
    i = src.index("async def _save_voice_recording")
    return src[i:i + 4200]


def test_auth_endpoint_preferred_with_fallback():
    body = _fn_body()
    assert 'os.getenv("VAPI_API_KEY"' in body
    assert "api.vapi.ai/call/" in body and "mono-recording" in body and "stereo-recording" in body
    assert "Authorization" in body and "Bearer" in body
    assert "follow_redirects=True" in body  # 302 → signed URL
    # 認証優先 → legacy fallback の順
    assert body.index("mono-recording") < body.index("r = await http.get(url")


def test_failure_is_loud():
    body = _fn_body()
    assert "loud_fail" in body and "VAPI_API_KEY" in body
    assert "voice_recording_download" in body


def test_caller_passes_call_id():
    src = (_ROOT / "main.py").read_text(encoding="utf-8")
    assert "bg_tasks.add_task(_save_voice_recording, request.app, raw_path.stem," in src
    # rec_url 無しでも call_id があれば取得を試みる (7/15 以降 webhook URL は non-fetchable)
    assert "if rec_url or call_id:" in src
