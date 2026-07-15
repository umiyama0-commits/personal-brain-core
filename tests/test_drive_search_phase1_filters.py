"""tests/test_drive_search_phase1_filters.py — Phase 1 海山指示 filter 改善

★2026-05-26 海山指示「データ膨大で見つからない、output 乱れる」 対応:
- since_days (default 365) + mime_filter (= sheets/docs/slides/PDF)
- top 5 default
- 全期間/全 type で「拡大検索」 fallback (= --all option)
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


# google package 必要 (= gdrive_sync import)。MacBook では skip。
try:
    import google.auth  # noqa: F401
    _HAS_GOOGLE = True
except ImportError:
    _HAS_GOOGLE = False
needs_google = pytest.mark.skipif(not _HAS_GOOGLE, reason="google-api-python-client 未 install (= local MacBook)")


@needs_google
def test_discover_since_days_applied():
    """since_days 指定で q に modifiedTime > <cutoff> 含まれる"""
    import gdrive_sync as gs

    captured = {}

    def fake_list(**kwargs):
        captured.update(kwargs)
        m = MagicMock()
        m.execute = lambda: {"files": []}
        return m

    fake_drive = MagicMock()
    fake_drive.files().list = fake_list
    with patch.object(gs, "get_credentials", return_value=MagicMock()), \
         patch.object(gs, "build", return_value=fake_drive):
        gs.discover("test", mode="fulltext", since_days=365, apply_default_exclude=False)

    assert "modifiedTime > '" in captured["q"]


@needs_google
def test_discover_mime_filter_applied():
    """mime_filter で q に mimeType= の OR 句 含まれる"""
    import gdrive_sync as gs

    captured = {}

    def fake_list(**kwargs):
        captured.update(kwargs)
        m = MagicMock()
        m.execute = lambda: {"files": []}
        return m

    fake_drive = MagicMock()
    fake_drive.files().list = fake_list
    with patch.object(gs, "get_credentials", return_value=MagicMock()), \
         patch.object(gs, "build", return_value=fake_drive):
        gs.discover(
            "test", mode="fulltext",
            mime_filter=list(gs.BOT_SEARCH_DEFAULT_MIMES),
            apply_default_exclude=False,
        )

    assert "mimeType='application/vnd.google-apps.spreadsheet'" in captured["q"]
    assert "mimeType='application/pdf'" in captured["q"]
    assert " or " in captured["q"]


@needs_google
def test_discover_no_filters_when_disabled():
    """since_days=None + mime_filter=None なら q に modifiedTime / mimeType 含まない"""
    import gdrive_sync as gs

    captured = {}

    def fake_list(**kwargs):
        captured.update(kwargs)
        m = MagicMock()
        m.execute = lambda: {"files": []}
        return m

    fake_drive = MagicMock()
    fake_drive.files().list = fake_list
    with patch.object(gs, "get_credentials", return_value=MagicMock()), \
         patch.object(gs, "build", return_value=fake_drive):
        gs.discover("test", mode="fulltext", apply_default_exclude=False)

    assert "modifiedTime" not in captured["q"]
    assert "mimeType" not in captured["q"]


@pytest.fixture
def gemini_mod(monkeypatch):
    monkeypatch.setenv("GEMINI_API_KEY", "fake")
    if "services.gemini_query" in sys.modules:
        del sys.modules["services.gemini_query"]
    return importlib.import_module("services.gemini_query")


@needs_google
@pytest.mark.asyncio
async def test_search_drive_semantic_default_filters(gemini_mod, monkeypatch):
    """apply_default_filters=True で discover に since_days=365 + mime_filter 渡される"""
    mod = gemini_mod
    captured_args = []

    def fake_discover(query, mime, limit, mode, exc, since, mime_f, content_check=True):
        captured_args.append({
            "query": query, "since": since, "mime_filter": mime_f,
        })
        return []

    import gdrive_sync as gs
    monkeypatch.setattr(gs, "content_safe_filter", lambda files, max_workers=6: files, raising=False)
    monkeypatch.setattr(gs, "discover", fake_discover)

    # expand_query を空で固定 (= keyword 拡張 skip、シンプルに)
    async def fake_expand(q, max_keywords=5):
        return {"must": [], "keywords": []}
    monkeypatch.setattr(mod, "expand_query_structured", fake_expand)

    result = await mod.search_drive_semantic(
        "売上", top_n=5, apply_default_filters=True,
    )
    # discover が 1 回呼ばれた (= keyword 空のため元 query 1 回のみ)
    assert len(captured_args) == 1
    assert captured_args[0]["since"] == 365
    assert captured_args[0]["mime_filter"] is not None
    assert "application/pdf" in captured_args[0]["mime_filter"]
    assert result["filters_applied"]["since_days"] == 365
    assert result["filters_applied"]["default_filters_on"] is True


@needs_google
@pytest.mark.asyncio
async def test_search_drive_semantic_no_filters_when_disabled(gemini_mod, monkeypatch):
    """apply_default_filters=False で since_days + mime_filter が None になる"""
    mod = gemini_mod
    captured_args = []

    def fake_discover(query, mime, limit, mode, exc, since, mime_f, content_check=True):
        captured_args.append({"since": since, "mime_filter": mime_f})
        return []

    import gdrive_sync as gs
    monkeypatch.setattr(gs, "content_safe_filter", lambda files, max_workers=6: files, raising=False)
    monkeypatch.setattr(gs, "discover", fake_discover)

    async def fake_expand(q, max_keywords=5):
        return {"must": [], "keywords": []}
    monkeypatch.setattr(mod, "expand_query_structured", fake_expand)

    result = await mod.search_drive_semantic(
        "売上", apply_default_filters=False,
    )
    assert captured_args[0]["since"] is None
    assert captured_args[0]["mime_filter"] is None
    assert result["filters_applied"]["default_filters_on"] is False


@needs_google
@pytest.mark.asyncio
async def test_search_drive_semantic_default_top_n_5(gemini_mod, monkeypatch):
    """default top_n が 5 (= 旧 3 から拡大)"""
    mod = gemini_mod

    def fake_discover(query, mime, limit, mode, exc, since, mime_f, content_check=True):
        return [
            {"id": str(i), "name": f"file{i}", "mimeType": "application/pdf",
             "modifiedTime": "2026-05-20", "webViewLink": "http://x"}
            for i in range(10)
        ]
    import gdrive_sync as gs
    monkeypatch.setattr(gs, "content_safe_filter", lambda files, max_workers=6: files, raising=False)
    monkeypatch.setattr(gs, "discover", fake_discover)

    async def fake_expand(q, max_keywords=5):
        return {"must": [], "keywords": []}
    monkeypatch.setattr(mod, "expand_query_structured", fake_expand)

    # rerank も mock (= top 5 を返す)
    async def fake_rerank(query, files, top_n=5, evidence=None, must_terms=None):
        return files[:top_n]
    monkeypatch.setattr(mod, "rerank_results", fake_rerank)

    result = await mod.search_drive_semantic("test")
    assert len(result["top"]) == 5


@needs_google
@pytest.mark.asyncio
async def test_search_drive_semantic_returns_filters_in_result(gemini_mod, monkeypatch):
    """戻り値に filters_applied が含まれる"""
    mod = gemini_mod
    import gdrive_sync as gs
    monkeypatch.setattr(gs, "content_safe_filter", lambda files, max_workers=6: files, raising=False)
    monkeypatch.setattr(gs, "discover", lambda *a, **k: [])

    async def fake_expand(q, max_keywords=5):
        return {"must": [], "keywords": []}
    monkeypatch.setattr(mod, "expand_query_structured", fake_expand)

    result = await mod.search_drive_semantic("test")
    assert "filters_applied" in result
    assert result["filters_applied"]["default_filters_on"] is True


def test_brain_commands_drive_ai_help():
    """LINE Bot /drive ai help に --all option が記載 + filter 説明"""
    src = (REPO_ROOT / "brain_commands.py").read_text(encoding="utf-8")
    assert "--all" in src
    # filter 表示 / oversaturated 警告 文言確認
    assert "拡大検索" in src or "全期間" in src
    assert "/drive ai" in src


def test_bot_search_default_mimes_includes_pdf():
    """★海山指示: PDF も含める"""
    src = (REPO_ROOT / "gdrive_sync.py").read_text(encoding="utf-8")
    assert "application/pdf" in src
    assert "BOT_SEARCH_DEFAULT_MIMES" in src


def test_bot_search_default_mimes_includes_office():
    """★2026-06-07 海山指示「サンプルリンク新規出店PJ (.pptx) が出ない」修正:
    Office 形式 (.pptx/.xlsx/.docx) を default 検索対象に追加。
    (= bot は BINARY_DOWNLOAD で既に取込・extract 済なのに検索不可だった不整合の解消)
    tuple 本体に限定して照合 (= BINARY_DOWNLOAD 側の同 MIME を誤検出しないため)。"""
    src = (REPO_ROOT / "gdrive_sync.py").read_text(encoding="utf-8")
    # tuple の閉じ括弧は行頭 "\n)" (= コメント内の inline ")" で切らないため)
    block = src.split("BOT_SEARCH_DEFAULT_MIMES = (", 1)[1].split("\n)", 1)[0]
    assert "presentationml.presentation" in block, ".pptx (PowerPoint) を tuple に含むこと"
    assert "spreadsheetml.sheet" in block, ".xlsx (Excel) を tuple に含むこと"
    assert "wordprocessingml.document" in block, ".docx (Word) を tuple に含むこと"
    # 画像/動画/圧縮は引き続き除外 (= noise 削減の原則維持)
    assert "image/" not in block and "video/" not in block


def test_expand_query_prompt_prioritizes_proper_nouns():
    """★2026-06-07 海山指示「Keyword 選定が質問の意図を捉えてない」修正:
    expand_query prompt が 固有名詞 (サンプルリンク 等) を最優先し、
    generic な場面語 (会議/議題) を検索対象から外すよう指示しているか (= keyword 抽出の主因 fix)。"""
    src = (REPO_ROOT / "services" / "gemini_query.py").read_text(encoding="utf-8")
    # 固有名詞-in-状況説明 の例 (例5) が存在
    assert "サンプルリンク" in src, "固有名詞-in-状況説明 の例 (例5) が存在すること"
    # 固有名詞最優先ルール
    assert "固有名詞" in src and "最優先" in src, "固有名詞を最優先する rule が存在"
    # generic 場面語の除外ルール
    assert "議題" in src or "アジェンダ" in src, "generic 場面語 (議題/アジェンダ) の除外言及"


def test_drive_alias_expansions(tmp_path, monkeypatch):
    """★2026-06-07 Phase1b (cross-check 反映): 海山承認済 (enabled=True) alias のみ検索 fan-out へ展開。
    未承認 (enabled=False) は無視 = verify-before-activate。bounded + dedup。"""
    import importlib
    monkeypatch.setenv("GEMINI_API_KEY", "x")
    if "services.gemini_query" in sys.modules:
        del sys.modules["services.gemini_query"]
    g = importlib.import_module("services.gemini_query")

    af = tmp_path / "drive_search_aliases.json"
    monkeypatch.setattr(g, "DRIVE_ALIASES_PATH", str(af))

    # enabled=True → 別表記を展開
    af.write_text(json.dumps({"サンプルリンク": {"aliases": ["新規出店PJ", "新店出店PJ"], "enabled": True}},
                             ensure_ascii=False), encoding="utf-8")
    assert g._drive_alias_expansions("サンプルリンクって何", ["新店会議"]) == ["新規出店PJ", "新店出店PJ"]
    # 含まない → 空
    assert g._drive_alias_expansions("武蔵小山の売上", ["予算"]) == []
    # 既存 keyword と被る alias は dedup で除外
    assert g._drive_alias_expansions("サンプルリンク", ["新規出店PJ"]) == ["新店出店PJ"]
    # ★enabled=False (未承認) は無視 = 検索に効かない (誤リンク遮断)
    af.write_text(json.dumps({"サンプルリンク": {"aliases": ["新規出店PJ"], "enabled": False}},
                             ensure_ascii=False), encoding="utf-8")
    assert g._drive_alias_expansions("サンプルリンク", []) == []
    # file 空 → 空
    af.write_text("{}", encoding="utf-8")
    assert g._drive_alias_expansions("サンプルリンク", []) == []
