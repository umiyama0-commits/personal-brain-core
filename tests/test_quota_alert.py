"""quota_alert の純関数 test (枯渇分類 + cooldown)。

★2026-06-08 海山指示「各種 API の残高枯渇を自動連絡」の runtime error 検知。
「明確な枯渇は alert / transient な rate-limit は alert しない」を固定する (false alarm 回避が肝)。
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))

from quota_alert import classify_quota_error, should_alert  # noqa: E402


# ─── classify: 枯渇シグナル → 検知 ──────────────────────────

def test_402_is_depletion():
    assert classify_quota_error(402, "") is not None


def test_openai_insufficient_quota():
    body = '{"error":{"code":"insufficient_quota","message":"You exceeded your current quota"}}'
    assert classify_quota_error(429, body) is not None


def test_anthropic_credit_balance():
    assert classify_quota_error(400, "Your credit balance is too low to access the API") is not None


def test_quota_exceeded_phrase():
    assert classify_quota_error(429, "Quota exceeded for quota metric 'Generate requests'") is not None


def test_payment_required_text():
    assert classify_quota_error(429, "Payment Required: please add credits") is not None


# ─── classify: transient/不明 → None (alert しない) ──────────────────────────

def test_plain_rate_limit_not_depletion():
    # 純粋な per-minute rate limit は枯渇ではない → alert しない
    assert classify_quota_error(429, "Rate limit reached. Please retry after 20s") is None


def test_5xx_not_depletion():
    assert classify_quota_error(503, "service temporarily unavailable") is None


def test_401_not_depletion():
    assert classify_quota_error(401, "invalid api key") is None


def test_empty_body_non402():
    assert classify_quota_error(429, "") is None


# ─── should_alert: cooldown ──────────────────────────

def test_no_reason_no_alert():
    assert should_alert("openai", None, now=1000.0, state={}, cooldown_min=60) is False


def test_first_alert_fires():
    assert should_alert("openai", "x", now=1000.0, state={}, cooldown_min=60) is True


def test_within_cooldown_suppressed():
    # 最後の alert が 30 分前、cooldown 60 分 → 抑制
    state = {"openai": 1000.0}
    now = 1000.0 + 30 * 60
    assert should_alert("openai", "x", now=now, state=state, cooldown_min=60) is False


def test_after_cooldown_fires_again():
    state = {"openai": 1000.0}
    now = 1000.0 + 90 * 60
    assert should_alert("openai", "x", now=now, state=state, cooldown_min=60) is True


def test_per_provider_independent():
    # openai は cooldown 中でも cohere は別カウント
    state = {"openai": 1000.0}
    now = 1000.0 + 10 * 60
    assert should_alert("cohere", "x", now=now, state=state, cooldown_min=60) is True
