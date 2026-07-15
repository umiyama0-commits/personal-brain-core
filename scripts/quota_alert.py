#!/usr/bin/env python3
"""quota_alert.py — API 残高枯渇を runtime の error から検知して即 LINE 通知。

★2026-06-08 海山指示「各種 API の残高枯渇を自動連絡」の本命レイヤー。
残高 ping できる API は ElevenLabs/HeyGen のみ (external_credit_watchdog)。OpenAI/Anthropic/
Gemini/Cohere は残高 API が無いため、枯渇した**瞬間**の error code を捕まえて通知する。
これで残高 API の有無に関係なく全 provider をカバーし、12h cron を待たず即時に気付ける。

設計上の安全弁:
- classify_quota_error は純関数 (= testable)。transient な rate-limit (= 単なる 429) は alert
  しない (false alarm 回避)。402 / insufficient_quota / billing / credit / quota exceeded 等の
  **明確な枯渇シグナル**のみ alert。
- maybe_alert_quota は provider ごとに cooldown (default 60分) で連投を防ぎ、LINE push 失敗や
  state IO 失敗を握りつぶす (= 呼び出し元の LLM hot path を絶対に壊さない)。
- 呼び出し元は error 経路でのみ呼ぶ (成功 path には一切触れない)。
"""
from __future__ import annotations

import json
import logging
import time
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)

STATE_FILE = (
    Path(__file__).resolve().parent.parent / "data" / "brain" / "clone_improve"
    / "quota_alert_state.json"
)

# 明確な「枯渇 (= 残高/クレジット切れ)」シグナル。transient な per-minute rate-limit は含めない。
_DEPLETION_KEYWORDS = (
    "insufficient_quota", "insufficient quota", "exceeded your current quota",
    "credit balance", "out of credit", "out of credits", "quota exceeded",
    "insufficient_funds", "insufficient funds", "billing hard limit",
    "billing", "payment required", "negative balance",
)


def classify_quota_error(status_code: Optional[int], body_text: str) -> Optional[str]:
    """depletion (= 残高/クレジット枯渇) なら理由文字列、transient/不明なら None。

    - HTTP 402 (Payment Required) → 明確な billing 枯渇。
    - body に insufficient_quota / billing / credit / quota exceeded 等 → 枯渇。
    - 単なる 429 (rate limit) / 5xx / 401 等 → None (= alert しない、false alarm 回避)。
    """
    if status_code == 402:
        return "payment_required (HTTP 402)"
    t = (body_text or "").lower()
    for kw in _DEPLETION_KEYWORDS:
        if kw in t:
            return f"depletion signal: '{kw}'"
    return None


def _load_state() -> dict:
    try:
        if STATE_FILE.exists():
            return json.loads(STATE_FILE.read_text(encoding="utf-8"))
    except Exception:
        pass
    return {}


def _save_state(state: dict) -> None:
    STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
    STATE_FILE.write_text(json.dumps(state, ensure_ascii=False), encoding="utf-8")


def should_alert(provider: str, reason: Optional[str], now: float,
                 state: dict, cooldown_min: int) -> bool:
    """純粋な cooldown 判定 (= testable)。reason 無し or cooldown 中なら False。"""
    if not reason:
        return False
    if provider not in state:
        return True  # 過去 alert が無ければ必ず fire (絶対 now に依存しない)
    try:
        last = float(state.get(provider, 0))
    except (TypeError, ValueError):
        last = 0.0
    return (now - last) >= cooldown_min * 60


def maybe_alert_quota(provider: str, *, status_code: Optional[int] = None,
                      body_text: str = "", cooldown_min: int = 60) -> bool:
    """depletion を検知したら cooldown 付きで LINE 通知。返り値 True=今回 alert した。

    呼び出し元 (LLM hot path 等) を絶対に壊さないよう、全体を try/except で握りつぶす。
    """
    try:
        reason = classify_quota_error(status_code, body_text)
        if not reason:
            return False
        now = time.time()
        state = _load_state()
        if not should_alert(provider, reason, now, state, cooldown_min):
            return False
        state[provider] = now
        try:
            _save_state(state)
        except Exception as e:
            logger.warning(f"quota_alert state save failed: {e}")
        # LINE push (失敗しても握りつぶす)
        try:
            import sys
            _scripts = str(Path(__file__).resolve().parent)
            if _scripts not in sys.path:
                sys.path.insert(0, _scripts)
            from clone_improve_lib import line_push  # type: ignore
            # 枯渇時は bot silent fail 進行中 = 配達保証必須 → critical (LW fallback 可)
            line_push(critical=True, text=
                f"🚨 API 残高枯渇の疑い: {provider}\n"
                f"  {reason} (HTTP {status_code})\n"
                f"  → bot 応答が fail し始めている可能性。{provider} の残高/課金を至急確認してください。"
            )
            logger.warning(f"quota depletion alert sent: {provider} ({reason})")
        except Exception as e:
            logger.warning(f"quota_alert line_push failed: {e}")
        return True
    except Exception as e:
        logger.warning(f"maybe_alert_quota error: {e}")
        return False
