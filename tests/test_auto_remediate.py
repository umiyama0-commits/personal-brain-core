"""tests/test_auto_remediate.py — bot_uptime_monitor.py の auto-remediation テスト

★2026-05-25 海山指示「自動で修正するように」: bot_dead / webhook_silent 検知時に
docker compose restart line-bot を試行 + rate limit + 復旧判定 + LINE Push 文言切替。
subprocess.run を mock してロジックだけ検証。
"""
from __future__ import annotations

import importlib
import json
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "scripts"))


@pytest.fixture
def tmp_alert_log(tmp_path, monkeypatch):
    monkeypatch.setenv("BRAIN_APP_ROOT", str(tmp_path))
    monkeypatch.setenv("AUTO_REMEDIATE_ENABLED", "1")
    # ★2026-08-14: 実在の /tmp/brain_auto_deploy.lock を見に行かせない。auto_deploy が
    # lock を持っている Mac Studio で pytest を回すと、無関係な既存 test が flaky になる。
    monkeypatch.setenv("BRAIN_DEPLOY_LOCK", str(tmp_path / "nolock"))
    if "bot_uptime_monitor" in sys.modules:
        del sys.modules["bot_uptime_monitor"]
    mod = importlib.import_module("bot_uptime_monitor")
    mod.ALERT_LOG = tmp_path / "bot_uptime_alerts.jsonl"
    return mod


def test_count_recent_restarts_empty(tmp_alert_log):
    mod = tmp_alert_log
    assert mod._count_recent_restarts() == 0


def test_count_recent_restarts_with_history(tmp_alert_log):
    mod = tmp_alert_log
    now = datetime.now(mod.JST)
    entries = [
        # 2 件 within window
        {"ts": (now - timedelta(minutes=10)).isoformat(), "type": "auto_restart", "severity": "info"},
        {"ts": (now - timedelta(minutes=30)).isoformat(), "type": "auto_restart", "severity": "warning"},
        # 1 件 outside window
        {"ts": (now - timedelta(minutes=90)).isoformat(), "type": "auto_restart", "severity": "info"},
        # 別 type
        {"ts": (now - timedelta(minutes=5)).isoformat(), "type": "bot_dead", "severity": "critical"},
    ]
    mod.ALERT_LOG.write_text("\n".join(json.dumps(e, ensure_ascii=False) for e in entries) + "\n", encoding="utf-8")
    assert mod._count_recent_restarts(within_min=60) == 2


def test_attempt_restart_disabled(tmp_alert_log, monkeypatch):
    monkeypatch.setenv("AUTO_REMEDIATE_ENABLED", "0")
    # Reload to pick up env
    if "bot_uptime_monitor" in sys.modules:
        del sys.modules["bot_uptime_monitor"]
    mod = importlib.import_module("bot_uptime_monitor")
    mod.ALERT_LOG = tmp_alert_log.ALERT_LOG  # carry over
    r = mod.attempt_auto_restart(reason="bot_dead")
    assert r["attempted"] is False
    assert "AUTO_REMEDIATE_ENABLED=0" in r["detail"]


def test_attempt_restart_rate_limited(tmp_alert_log):
    mod = tmp_alert_log
    now = datetime.now(mod.JST)
    # 3 recent restarts → at limit
    entries = [
        {"ts": (now - timedelta(minutes=i*10)).isoformat(), "type": "auto_restart", "severity": "info"}
        for i in range(3)
    ]
    mod.ALERT_LOG.write_text("\n".join(json.dumps(e) for e in entries) + "\n", encoding="utf-8")
    r = mod.attempt_auto_restart(reason="bot_dead")
    assert r["attempted"] is False
    assert "rate limit" in r["detail"]


def test_attempt_restart_success_recovered(tmp_alert_log):
    mod = tmp_alert_log

    # Mock subprocess.run (= restart success) + check_health (= recovered)
    fake_result = MagicMock(returncode=0, stdout="ok", stderr="")
    with patch.object(mod.subprocess, "run", return_value=fake_result), \
         patch.object(mod, "check_health", return_value={"ok": True, "url": "http://localhost:8000/health"}), \
         patch.object(mod.time, "sleep"):
        r = mod.attempt_auto_restart(reason="bot_dead")

    assert r["attempted"] is True
    assert r["ok"] is True
    assert r["recovered"] is True
    # ALERT_LOG に auto_restart entry 残ってる
    log = mod.ALERT_LOG.read_text(encoding="utf-8").splitlines()
    last = json.loads(log[-1])
    assert last["type"] == "auto_restart"
    assert last["severity"] == "info"


def test_attempt_restart_success_not_recovered(tmp_alert_log):
    """restart 成功したが /health まだ NG (= CF tunnel 死亡 等で restart 効かない case)"""
    mod = tmp_alert_log
    fake_result = MagicMock(returncode=0, stdout="", stderr="")
    with patch.object(mod.subprocess, "run", return_value=fake_result), \
         patch.object(mod, "check_health", return_value={"ok": False, "error": "still NG"}), \
         patch.object(mod.time, "sleep"):
        r = mod.attempt_auto_restart(reason="webhook_silent")

    assert r["attempted"] is True
    assert r["ok"] is True
    assert r["recovered"] is False
    log = mod.ALERT_LOG.read_text(encoding="utf-8").splitlines()
    last = json.loads(log[-1])
    assert last["type"] == "auto_restart"
    assert last["severity"] == "warning"


def test_attempt_restart_command_fails(tmp_alert_log):
    """docker compose restart 自体が non-zero → ok=False"""
    mod = tmp_alert_log
    fake_result = MagicMock(returncode=1, stdout="", stderr="container not found")
    with patch.object(mod.subprocess, "run", return_value=fake_result):
        r = mod.attempt_auto_restart(reason="bot_dead")

    assert r["attempted"] is True
    assert r["ok"] is False
    assert r["recovered"] is False
    assert "rc=1" in r["detail"]
    assert "container not found" in r["detail"]


def test_attempt_restart_docker_not_found(tmp_alert_log):
    mod = tmp_alert_log
    with patch.object(mod.subprocess, "run", side_effect=FileNotFoundError("docker")):
        r = mod.attempt_auto_restart(reason="bot_dead")
    assert r["attempted"] is True
    assert r["ok"] is False
    assert "docker not found" in r["detail"]


def test_attempt_restart_timeout(tmp_alert_log):
    mod = tmp_alert_log
    with patch.object(mod.subprocess, "run",
                      side_effect=mod.subprocess.TimeoutExpired("docker", 60)):
        r = mod.attempt_auto_restart(reason="bot_dead")
    assert r["attempted"] is True
    assert r["ok"] is False
    assert "timeout" in r["detail"]


# ─── メンテ lock 尊重 (★2026-08-14 chroma 計画 rebuild) ───
def test_deploy_lock_held_when_fresh(tmp_alert_log, tmp_path):
    """auto_deploy / 計画 rebuild が lock を持っている間は保持中と判定する。"""
    mod = tmp_alert_log
    lock = tmp_path / "brain_auto_deploy.lock"
    lock.mkdir()
    mod.DEPLOY_LOCK_DIR = lock
    assert mod._deploy_lock_held() != ""


def test_deploy_lock_ignored_when_stale(tmp_alert_log, tmp_path):
    """30 分超の残骸 lock は無視 = 置き去り lock で自動復旧が永久に死なない。"""
    import os as _os
    import time as _time
    mod = tmp_alert_log
    lock = tmp_path / "brain_auto_deploy.lock"
    lock.mkdir()
    old = _time.time() - 45 * 60
    _os.utime(lock, (old, old))
    mod.DEPLOY_LOCK_DIR = lock
    assert mod._deploy_lock_held() == ""


def test_deploy_lock_absent(tmp_alert_log, tmp_path):
    mod = tmp_alert_log
    mod.DEPLOY_LOCK_DIR = tmp_path / "no-such-lock"
    assert mod._deploy_lock_held() == ""


def test_attempt_restart_skipped_during_maintenance(tmp_alert_log, tmp_path):
    """rebuild 中の 5 分 cron が restart を撃たない (削除途中の索引で chroma を開かせない)。"""
    mod = tmp_alert_log
    lock = tmp_path / "brain_auto_deploy.lock"
    lock.mkdir()
    mod.DEPLOY_LOCK_DIR = lock
    with patch.object(mod.subprocess, "run") as run:
        r = mod.attempt_auto_restart(reason="bot_dead")
    run.assert_not_called()
    assert r["attempted"] is False
    assert "lock" in r["detail"]
