"""tests/test_auto_deploy_triggers.py — auto_deploy.sh の即時再生成 trigger 検証

★2026-05-25 海山指示「macbook から完結させて」: MacBook → push → auto_deploy が
Mac Studio で自動 pull + rebuild + wiki 再生成まで完結する flow を保証。
"""
from __future__ import annotations

import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))


def test_auto_deploy_has_monday_dash_trigger():
    """auto_deploy.sh が scripts/build_monday_dash_latest.py 変更時に再生成する"""
    src = (REPO_ROOT / "scripts" / "auto_deploy.sh").read_text()
    assert "MONDAY_DASH_CHANGED" in src
    assert "build_monday_dash_latest.py" in src
    # 再生成 実行 block
    assert 'python3 scripts/build_monday_dash_latest.py' in src


def test_auto_deploy_has_stores_range_trigger():
    """auto_deploy.sh が scripts/build_stores_by_customer_range.py 変更時に再生成する"""
    src = (REPO_ROOT / "scripts" / "auto_deploy.sh").read_text()
    assert "STORES_RANGE_CHANGED" in src
    assert "build_stores_by_customer_range.py" in src
    assert 'python3 scripts/build_stores_by_customer_range.py' in src


def test_stores_by_range_cron_wrapper_exists():
    """stores_by_customer_range cron wrapper script が存在 + executable"""
    wrap = REPO_ROOT / "scripts" / "stores_by_customer_range_cron.sh"
    assert wrap.exists(), f"missing: {wrap}"
    content = wrap.read_text()
    assert content.startswith("#!/"), "shebang missing"
    assert "cron_env.sh" in content, "cron_env.sh source 必須 (CLAUDE.md 1.8)"
    assert "build_stores_by_customer_range.py" in content


def test_cron_install_registers_stores_range():
    """scripts/cron_install.sh に stores-by-range の daily cron 登録"""
    src = (REPO_ROOT / "scripts" / "cron_install.sh").read_text()
    assert "stores_by_customer_range_cron.sh" in src
    # 23:35 daily 想定
    assert "35 23 * * *" in src, "daily 23:35 entry not registered"
