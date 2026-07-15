"""tests/test_drive_search_phase1_2.py — Drive 検索 Phase 1 (fullText) + Phase 2 (Gemini augment)

★2026-05-26 海山指示「Gemini Workspace 連携で Drive 内検索 bot 経由」:
- Phase 1: gdrive_sync.discover() に mode='fulltext' で name + 中身 検索
- Phase 2: services/gemini_query で query 拡張 + re-rank
"""
from __future__ import annotations

import importlib
import json
import sys
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

# google package が無い MacBook local では gdrive_sync test を skip
# (Mac Studio docker では入ってる、本番で通る)
try:
    import google.auth  # noqa: F401
    _HAS_GOOGLE = True
except ImportError:
    _HAS_GOOGLE = False
needs_google = pytest.mark.skipif(not _HAS_GOOGLE, reason="google-api-python-client 未 install (= local MacBook)")


# ─── Phase 1: gdrive_sync.discover() fullText mode ─────────
@needs_google
def test_discover_mode_fulltext_query():
    """mode='fulltext' で q 文字列に fullText contains が含まれる"""
    import gdrive_sync as gs

    captured = {}

    def fake_files_list(**kwargs):
        captured.update(kwargs)
        m = MagicMock()
        m.execute = lambda: {"files": []}
        return m

    fake_drive = MagicMock()
    fake_drive.files().list = fake_files_list

    with patch.object(gs, "get_credentials", return_value=MagicMock()), \
         patch.object(gs, "build", return_value=fake_drive):
        gs.discover("武蔵小山", mode="fulltext", apply_default_exclude=False)

    assert "fullText contains '武蔵小山'" in captured["q"]
    assert "name contains '武蔵小山'" in captured["q"]


@needs_google
def test_discover_mode_name_only():
    """mode='name' (= 旧 default) で fullText 含まれない"""
    import gdrive_sync as gs

    captured = {}

    def fake_files_list(**kwargs):
        captured.update(kwargs)
        m = MagicMock()
        m.execute = lambda: {"files": []}
        return m

    fake_drive = MagicMock()
    fake_drive.files().list = fake_files_list
    with patch.object(gs, "get_credentials", return_value=MagicMock()), \
         patch.object(gs, "build", return_value=fake_drive):
        gs.discover("monday dash", mode="name", apply_default_exclude=False)

    assert "name contains 'monday dash'" in captured["q"]
    assert "fullText contains" not in captured["q"]


@needs_google
def test_discover_default_exclude_filter_works():
    """DEFAULT_EXCLUDE_PATTERN match (人事評価 等) は結果から fail-safe 除外"""
    import gdrive_sync as gs

    fake_results = {
        "files": [
            {"id": "1", "name": "Monday Dash 2026-05-26.xlsx", "mimeType": "application/vnd.google-apps.spreadsheet"},
            {"id": "2", "name": "人事評価 2026.xlsx", "mimeType": "application/vnd.google-apps.spreadsheet"},
            {"id": "3", "name": "営業実績.xlsx", "mimeType": "application/vnd.google-apps.spreadsheet"},
            {"id": "4", "name": "個人評価_田中.docx", "mimeType": "application/vnd.google-apps.document"},
        ]
    }
    fake_drive = MagicMock()
    fake_drive.files().list().execute = lambda: fake_results

    with patch.object(gs, "get_credentials", return_value=MagicMock()), \
         patch.object(gs, "build", return_value=fake_drive):
        result = gs.discover("dummy", mode="fulltext", apply_default_exclude=True)

    ids = {f["id"] for f in result}
    assert "1" in ids  # Monday Dash, safe
    assert "3" in ids  # 営業実績, safe
    assert "2" not in ids  # 人事評価 EXCLUDE
    assert "4" not in ids  # 個人評価 EXCLUDE


@needs_google
def test_discover_no_exclude_when_disabled():
    """apply_default_exclude=False なら filter しない"""
    import gdrive_sync as gs
    fake_results = {
        "files": [
            {"id": "1", "name": "人事評価.xlsx", "mimeType": "x"},
        ]
    }
    fake_drive = MagicMock()
    fake_drive.files().list().execute = lambda: fake_results

    with patch.object(gs, "get_credentials", return_value=MagicMock()), \
         patch.object(gs, "build", return_value=fake_drive):
        result = gs.discover("x", mode="fulltext", apply_default_exclude=False)
    assert len(result) == 1
    assert result[0]["id"] == "1"


# ─── Phase 2: gemini_query ─────────
@pytest.fixture
def gemini_mod(monkeypatch):
    monkeypatch.setenv("GEMINI_API_KEY", "fake-test-key")
    if "services.gemini_query" in sys.modules:
        del sys.modules["services.gemini_query"]
    return importlib.import_module("services.gemini_query")


@pytest.mark.asyncio
async def test_expand_query_basic(gemini_mod, monkeypatch):
    """Gemini が JSON で 4 keyword 返す mock"""
    mod = gemini_mod
    async def fake_generate(prompt, response_json=False, max_tokens=512):
        return '["武蔵小山", "予算", "FY26", "5月"]'
    monkeypatch.setattr(mod, "_generate", fake_generate)
    result = await mod.expand_query("武蔵小山店の今月予算は?")
    assert result == ["武蔵小山", "予算", "FY26", "5月"]


@pytest.mark.asyncio
async def test_expand_query_no_api_key(monkeypatch):
    """GEMINI_API_KEY 無し → 空 list (= caller 側 fallback)"""
    monkeypatch.delenv("GEMINI_API_KEY", raising=False)
    if "services.gemini_query" in sys.modules:
        del sys.modules["services.gemini_query"]
    import services.gemini_query as mod
    result = await mod.expand_query("test")
    assert result == []


@pytest.mark.asyncio
async def test_expand_query_invalid_json(gemini_mod, monkeypatch):
    """Gemini が変な response 返した場合は 空 list"""
    mod = gemini_mod
    async def fake_generate(prompt, response_json=False, max_tokens=512):
        return "not json at all"
    monkeypatch.setattr(mod, "_generate", fake_generate)
    result = await mod.expand_query("test")
    assert result == []


@pytest.mark.asyncio
async def test_rerank_results_picks_top_n(gemini_mod, monkeypatch):
    """Gemini が index 指定で top 3 を返す"""
    mod = gemini_mod
    files = [
        {"id": str(i), "name": f"file{i}", "modifiedTime": "2026-05-2{i}T10:00:00Z"}
        for i in range(1, 6)
    ]
    async def fake_generate(prompt, response_json=False, max_tokens=512):
        return json.dumps([
            {"index": 2, "reason": "売上 sheet"},
            {"index": 4, "reason": "予算 doc"},
            {"index": 1, "reason": "Monday Dash"},
        ])
    monkeypatch.setattr(mod, "_generate", fake_generate)
    result = await mod.rerank_results("売上", files, top_n=3)
    assert len(result) == 3
    assert result[0]["id"] == "2"
    assert result[0]["rerank_reason"] == "売上 sheet"
    assert result[1]["id"] == "4"


@pytest.mark.asyncio
async def test_rerank_fallback_when_gemini_fails(gemini_mod, monkeypatch):
    """Gemini API 失敗 → modifiedTime DESC で fallback"""
    mod = gemini_mod
    files = [
        {"id": "a", "name": "old", "modifiedTime": "2026-01-01"},
        {"id": "b", "name": "newest", "modifiedTime": "2026-05-26"},
        {"id": "c", "name": "mid", "modifiedTime": "2026-03-15"},
    ]
    async def fake_generate(prompt, response_json=False, max_tokens=512):
        raise mod.GeminiUnavailableError("network error")
    monkeypatch.setattr(mod, "_generate", fake_generate)
    result = await mod.rerank_results("test", files, top_n=2)
    assert len(result) == 2
    assert result[0]["id"] == "b"  # newest first
    assert result[1]["id"] == "c"


@pytest.mark.asyncio
async def test_rerank_empty_input(gemini_mod):
    mod = gemini_mod
    result = await mod.rerank_results("test", [], top_n=3)
    assert result == []


@pytest.mark.asyncio
async def test_rerank_returns_all_when_few_files(gemini_mod):
    """files <= top_n なら sort なしでそのまま"""
    mod = gemini_mod
    files = [{"id": "1", "name": "a"}, {"id": "2", "name": "b"}]
    result = await mod.rerank_results("x", files, top_n=3)
    assert len(result) == 2


def test_drive_search_help_includes_ai_command():
    """LINE Bot help に /drive ai が記載されてる"""
    src = (REPO_ROOT / "brain_commands.py").read_text(encoding="utf-8")
    assert "/drive ai" in src
    assert "Gemini augmented" in src or "Gemini" in src


def test_env_example_has_gemini_key():
    src = (REPO_ROOT / ".env.example").read_text(encoding="utf-8")
    assert "GEMINI_API_KEY" in src
    assert "aistudio.google.com" in src
