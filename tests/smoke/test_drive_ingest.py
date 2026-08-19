"""smoke test: services/drive_ingest.py (★2026-05-23 海山指示)

URL 検出 + truncate + clone_respond_public 統合の sanity check。
実 Drive API 呼出は mock 化、network 無しで test。
"""
from __future__ import annotations

from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent.parent


# ─── URL 検出 ─────────────
@pytest.mark.smoke
def test_extract_drive_urls_presentation():
    from services.drive_ingest import extract_drive_urls
    text = "資料はこれ https://docs.google.com/presentation/d/1_l8_rMCwt6AAYbbVXjA8-WW6SSiZehZllMN1YDgnQVY/edit?usp=sharing"
    urls = extract_drive_urls(text)
    assert len(urls) == 1
    assert "presentation/d/1_l8_rMCwt6AAYbbVXjA8-WW6SSiZehZllMN1YDgnQVY" in urls[0]


@pytest.mark.smoke
def test_extract_drive_urls_multiple_types():
    from services.drive_ingest import extract_drive_urls
    text = """
    Docs: https://docs.google.com/document/d/aaaaaaaaaaaaaaaaaaaaaaaa/edit
    Slides: https://docs.google.com/presentation/d/bbbbbbbbbbbbbbbbbbbbbbbb/edit
    Sheets: https://docs.google.com/spreadsheets/d/cccccccccccccccccccccccc/edit
    Drive file: https://drive.google.com/file/d/dddddddddddddddddddddddd/view
    """
    urls = extract_drive_urls(text)
    assert len(urls) == 4


@pytest.mark.smoke
def test_extract_drive_urls_dedup():
    """同じ URL が複数回出てきても 1 件に dedup。"""
    from services.drive_ingest import extract_drive_urls
    url = "https://docs.google.com/presentation/d/abcdefghijklmnopqrstuvwx/edit"
    text = f"{url} と {url} を見て"
    urls = extract_drive_urls(text)
    assert len(urls) == 1


@pytest.mark.smoke
def test_extract_drive_urls_no_match():
    from services.drive_ingest import extract_drive_urls
    assert extract_drive_urls("普通のテキスト") == []
    assert extract_drive_urls("https://example.com/foo") == []
    # google.com だが drive ではない
    assert extract_drive_urls("https://google.com/search?q=foo") == []


# ─── ID 抽出 + truncate ─────────────
@pytest.mark.smoke
def test_extract_id():
    from services.drive_ingest import _extract_id
    url = "https://docs.google.com/presentation/d/1_l8_rMCwt6AAYbbVXjA8/edit?usp=sharing"
    assert _extract_id(url) == "1_l8_rMCwt6AAYbbVXjA8"


@pytest.mark.smoke
def test_truncate_keeps_short_text():
    from services.drive_ingest import _truncate
    short = "短い本文"
    assert _truncate(short, max_chars=100) == short


@pytest.mark.smoke
def test_truncate_cuts_long_text():
    from services.drive_ingest import _truncate
    long_text = "あ" * 10_000
    out = _truncate(long_text, max_chars=500)
    assert len(out) < 700  # truncate suffix 込み
    assert "truncated" in out


# ─── fetch 失敗時の silent skip ─────────────
@pytest.mark.smoke
def test_fetch_text_silent_on_credentials_missing(monkeypatch):
    """credentials 未設定なら silent skip (= ok=False, error 文言だけ)。"""
    from services.drive_ingest import fetch_text

    # gdrive_sync.get_credentials が例外を投げるよう mock
    import sys
    class _FakeGS:
        @staticmethod
        def get_credentials():
            raise FileNotFoundError("token.json not found")
    monkeypatch.setitem(sys.modules, "gdrive_sync", _FakeGS)

    r = fetch_text("https://docs.google.com/presentation/d/aaaaaaaaaaaaaaaaaaaaaaaa/edit")
    assert r["ok"] is False
    assert "credentials unavailable" in r["error"]
    # title / text は空
    assert r["title"] == ""
    assert r["text"] == ""


@pytest.mark.smoke
def test_fetch_text_invalid_url():
    """ID 抽出できない URL は即 silent skip。"""
    from services.drive_ingest import fetch_text
    r = fetch_text("https://example.com/not-drive")
    assert r["ok"] is False
    assert "invalid url" in r["error"]


# ─── build_context_block ─────────────
@pytest.mark.smoke
def test_build_context_block_empty_when_no_urls():
    from services.drive_ingest import build_context_block
    assert build_context_block("こんにちは、元気？") == ""


@pytest.mark.smoke
def test_build_context_block_empty_when_all_fetches_fail(monkeypatch):
    """URL あるが全 fetch 失敗 → 空文字 (= LLM が「開けない」と返す現挙動維持)。"""
    from services import drive_ingest as di

    def _fake_fetch(url, max_chars=5000):
        return {"ok": False, "url": url, "title": "", "mime": "", "text": "", "error": "401"}
    monkeypatch.setattr(di, "fetch_text", _fake_fetch)

    out = di.build_context_block(
        "資料: https://docs.google.com/presentation/d/aaaaaaaaaaaaaaaaaaaaaaaa/edit"
    )
    assert out == ""


@pytest.mark.smoke
def test_build_context_block_success(monkeypatch):
    """fetch 成功 → 【共有された資料】ブロックが返る。"""
    from services import drive_ingest as di

    def _fake_fetch(url, max_chars=5000):
        return {
            "ok": True, "url": url,
            "title": "FY27 AOP Draft",
            "mime": "application/vnd.google-apps.presentation",
            "text": "売上目標: 200 億\n粗利率: 60%\n...",
            "error": "",
        }
    monkeypatch.setattr(di, "fetch_text", _fake_fetch)

    out = di.build_context_block(
        "これ見て https://docs.google.com/presentation/d/aaaaaaaaaaaaaaaaaaaaaaaa/edit"
    )
    assert "【共有された資料】" in out
    assert "FY27 AOP Draft" in out
    assert "売上目標: 200 億" in out
    assert out.endswith("---\n\n")


# ─── 権限不足時の共有依頼テンプレ (★2026-05-23 海山指示) ─────────────
@pytest.mark.smoke
def test_build_context_block_permission_denied_with_address(monkeypatch):
    """403 returned → 「viewer として <BOT_SHARE_ADDRESS> に共有して」テンプレを context に。"""
    import importlib
    monkeypatch.setenv("BOT_GDRIVE_SHARE_ADDRESS", "bot-account@example.co.jp")
    from services import drive_ingest as di
    importlib.reload(di)

    def _fake_fetch(url, max_chars=5000):
        return {
            "ok": False, "url": url, "title": "", "mime": "",
            "text": "", "error": "permission denied",
            "error_code": "permission_denied",
        }
    monkeypatch.setattr(di, "fetch_text", _fake_fetch)

    out = di.build_context_block(
        "見て https://docs.google.com/presentation/d/aaaaaaaaaaaaaaaaaaaaaaaa/edit"
    )
    assert "【閲覧権限が無い資料】" in out
    assert "bot-account@example.co.jp" in out
    assert "viewer" in out or "閲覧者" in out


@pytest.mark.smoke
def test_build_context_block_permission_denied_without_address(monkeypatch):
    """env 未設定なら「公開範囲を見直すか PDF で投げて」テンプレ。"""
    import importlib
    monkeypatch.delenv("BOT_GDRIVE_SHARE_ADDRESS", raising=False)
    from services import drive_ingest as di
    importlib.reload(di)

    def _fake_fetch(url, max_chars=5000):
        return {
            "ok": False, "url": url, "title": "", "mime": "",
            "text": "", "error": "permission denied",
            "error_code": "permission_denied",
        }
    monkeypatch.setattr(di, "fetch_text", _fake_fetch)

    out = di.build_context_block(
        "見て https://docs.google.com/presentation/d/aaaaaaaaaaaaaaaaaaaaaaaa/edit"
    )
    assert "【閲覧権限が無い資料】" in out
    assert "公開範囲" in out or "PDF" in out


@pytest.mark.smoke
def test_build_context_block_not_found_silent(monkeypatch):
    """not_found / credentials_missing は silent skip (= 空文字)、共有依頼テンプレは出さない。"""
    from services import drive_ingest as di

    def _fake_fetch(url, max_chars=5000):
        return {
            "ok": False, "url": url, "title": "", "mime": "",
            "text": "", "error": "not found", "error_code": "not_found",
        }
    monkeypatch.setattr(di, "fetch_text", _fake_fetch)

    out = di.build_context_block(
        "見て https://docs.google.com/presentation/d/aaaaaaaaaaaaaaaaaaaaaaaa/edit"
    )
    assert out == ""


@pytest.mark.smoke
def test_build_context_block_mixed_success_and_permission(monkeypatch):
    """成功 1 件 + 権限不足 1 件 → 両方 context に乗る。"""
    import importlib
    monkeypatch.setenv("BOT_GDRIVE_SHARE_ADDRESS", "bot@example.com")
    from services import drive_ingest as di
    importlib.reload(di)

    call_count = {"n": 0}

    def _fake_fetch(url, max_chars=5000):
        call_count["n"] += 1
        if call_count["n"] == 1:
            return {
                "ok": True, "url": url, "title": "FY27 AOP", "mime": "presentation",
                "text": "売上 200億", "error": "", "error_code": "other",
            }
        return {
            "ok": False, "url": url, "title": "", "mime": "",
            "text": "", "error": "permission denied",
            "error_code": "permission_denied",
        }
    monkeypatch.setattr(di, "fetch_text", _fake_fetch)

    text = (
        "https://docs.google.com/presentation/d/aaaaaaaaaaaaaaaaaaaaaaaa/edit と "
        "https://docs.google.com/presentation/d/bbbbbbbbbbbbbbbbbbbbbbbb/edit"
    )
    out = di.build_context_block(text)
    assert "【共有された資料】" in out and "FY27 AOP" in out
    assert "【閲覧権限が無い資料】" in out and "bot@example.com" in out


# ─── error_code 切り分け ─────────────
@pytest.mark.smoke
def test_fetch_text_credentials_missing_error_code(monkeypatch):
    import sys
    class _FakeGS:
        @staticmethod
        def get_credentials():
            raise FileNotFoundError("token.json not found")
    monkeypatch.setitem(sys.modules, "gdrive_sync", _FakeGS)
    from services.drive_ingest import fetch_text
    r = fetch_text("https://docs.google.com/presentation/d/aaaaaaaaaaaaaaaaaaaaaaaa/edit")
    assert r["error_code"] == "credentials_missing"


@pytest.mark.smoke
def test_fetch_text_invalid_url_error_code():
    from services.drive_ingest import fetch_text
    r = fetch_text("https://example.com/not-drive")
    assert r["error_code"] == "invalid_url"


# ─── brain_wiki.clone_respond_public 統合 ─────────────
@pytest.mark.smoke
def test_brain_wiki_imports_drive_ingest():
    """brain_wiki.py が services.drive_ingest.build_context_block を呼ぶ。"""
    src = (REPO / "brain_wiki.py").read_text(encoding="utf-8")
    assert "from services.drive_ingest import build_context_block" in src
    assert "drive_context_block" in src
