"""smoke test: gitleaks config (★2026-05-23 LEE §4.1)。

pre-commit + .gitleaks.toml が正しく設定され、PB 固有 pattern (Owndays / sk-litellm)
を block する事を確認。gitleaks 自体は CI で動くため、ここは config 構造の sanity check。
"""
from __future__ import annotations

from pathlib import Path

import pytest


REPO = Path(__file__).resolve().parent.parent.parent


@pytest.mark.smoke
def test_pre_commit_config_has_gitleaks():
    """.pre-commit-config.yaml に gitleaks hook が登録されている。"""
    cfg = (REPO / ".pre-commit-config.yaml").read_text(encoding="utf-8")
    assert "gitleaks" in cfg
    assert "github.com/gitleaks/gitleaks" in cfg
    assert "id: gitleaks" in cfg


@pytest.mark.smoke
def test_gitleaks_toml_exists():
    """.gitleaks.toml 設定ファイルが存在する。"""
    path = REPO / ".gitleaks.toml"
    assert path.exists()
    txt = path.read_text(encoding="utf-8")
    # 必要な部分
    assert "useDefault = true" in txt
    assert "[[rules]]" in txt


@pytest.mark.smoke
def test_gitleaks_detects_owndays_pattern():
    """OWNDAYS password pattern が rule に含まれている。"""
    cfg = (REPO / ".gitleaks.toml").read_text(encoding="utf-8")
    assert "owndays-mobile-password" in cfg
    assert "Owndays[0-9]{3,}" in cfg


@pytest.mark.smoke
def test_gitleaks_detects_litellm_key():
    """LiteLLM master key pattern が rule に含まれている。"""
    cfg = (REPO / ".gitleaks.toml").read_text(encoding="utf-8")
    assert "litellm-master-key" in cfg
    assert "sk-litellm-" in cfg


@pytest.mark.smoke
def test_gitleaks_detects_stapa_hardcode():
    cfg = (REPO / ".gitleaks.toml").read_text(encoding="utf-8")
    assert "stapa-login-hardcode" in cfg
    assert "LOGIN_PASS" in cfg


@pytest.mark.smoke
def test_gitleaks_allowlist_includes_env_example():
    """.env.example 内の placeholder を allowlist している (= false positive 防止)。"""
    cfg = (REPO / ".gitleaks.toml").read_text(encoding="utf-8")
    assert "allowlist" in cfg
    assert ".env.example" in cfg
    assert "your-password" in cfg


@pytest.mark.smoke
def test_no_secret_in_main_branch_now():
    """現時点で repo に平文 secret が残っていないこと (= Step 1 効果検証)。

    git grep の代替で、tracked file を直接 walk して pattern check。
    """
    import re
    import subprocess
    # tracked file 一覧 (= .gitignore 反映済)
    out = subprocess.run(
        ["git", "ls-files"], cwd=REPO, capture_output=True, text=True
    )
    files = [f for f in out.stdout.splitlines() if f and not f.startswith("data/")]
    # 検出 pattern
    bad_patterns = [
        re.compile(r"Owndays[0-9]{3,}"),
        re.compile(r"sk-litellm-brain-[0-9]"),
        re.compile(r'LOGIN_PASS\s*=\s*"[^"]+"'),
    ]
    leaks = []
    for f in files:
        # binary や 自分自身 (test file / config) は除外
        if f.endswith((".png", ".jpg", ".jpeg", ".pdf", ".zip", ".tar.gz")):
            continue
        if f in {".gitleaks.toml", "tests/smoke/test_gitleaks_config.py"}:
            continue
        try:
            txt = (REPO / f).read_text(encoding="utf-8")
        except (UnicodeDecodeError, FileNotFoundError, IsADirectoryError):
            continue
        for pat in bad_patterns:
            m = pat.search(txt)
            if m:
                leaks.append(f"{f}: {m.group(0)[:50]}")
                break
    assert not leaks, f"平文 secret が残っている: {leaks}"
