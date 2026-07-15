"""smoke test: scripts/external_credit_watchdog.py (★2026-05-23 海山指示 (a))

外部 service 残高切れ silent fail を防ぐ daily 監視の構造 sanity。
API call は mock、purely 文字列構造 + cron 統合確認。
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(REPO / "scripts"))


@pytest.mark.smoke
def test_watchdog_script_imports():
    """script が import 可能 (= syntax OK)。"""
    import importlib
    import external_credit_watchdog
    importlib.reload(external_credit_watchdog)
    assert hasattr(external_credit_watchdog, "check_vapi")
    assert hasattr(external_credit_watchdog, "check_litellm")
    assert hasattr(external_credit_watchdog, "run_check")


@pytest.mark.smoke
def test_check_vapi_skipped_when_no_key(monkeypatch):
    """VAPI_PRIVATE_API_KEY 未設定なら skip return。"""
    monkeypatch.delenv("VAPI_PRIVATE_API_KEY", raising=False)
    import importlib
    import external_credit_watchdog
    importlib.reload(external_credit_watchdog)

    result = external_credit_watchdog.check_vapi()
    assert result["service"] == "vapi"
    assert result["ok"] is True
    assert "skipped" in result


@pytest.mark.smoke
def test_check_litellm_skipped_when_no_key(monkeypatch):
    """LITELLM_MASTER_KEY 未設定なら skip return。"""
    monkeypatch.delenv("LITELLM_MASTER_KEY", raising=False)
    import importlib
    import external_credit_watchdog
    importlib.reload(external_credit_watchdog)

    result = external_credit_watchdog.check_litellm()
    assert result["service"] == "litellm"
    assert result["ok"] is True
    assert "skipped" in result


@pytest.mark.smoke
def test_thresholds_are_sane():
    """env 由来 threshold が現実的な範囲。"""
    import external_credit_watchdog as w
    assert w.VAPI_LOW_BALANCE_USD > 0  # > $0
    assert 50 <= w.LITELLM_HIGH_USAGE_PCT <= 95  # 50-95%
    assert w.LITELLM_MAX_BUDGET > 0


@pytest.mark.smoke
def test_clone_cron_has_credit_check_mode():
    """clone_cron.sh が credit-check モードを処理する。"""
    src = (REPO / "scripts" / "clone_cron.sh").read_text(encoding="utf-8")
    assert "credit-check)" in src
    assert "external_credit_watchdog.py" in src


@pytest.mark.smoke
def test_cron_install_includes_credit_check():
    """cron_install.sh で credit-check が 09:05 + 21:05 daily で登録される。"""
    src = (REPO / "scripts" / "cron_install.sh").read_text(encoding="utf-8")
    assert "credit-check" in src
    # 朝夕 2 回 (= 9:05 + 21:05 で運用)
    assert "5 9,21 * * *" in src
    # PATTERNS にも入ってる
    assert "clone_cron.sh credit-check" in src
