"""
notebooklm_extractor.py — NotebookLM 共有ノートの抽出

NotebookLM (notebooklm.google.com) は Google 認証必須のため、
Playwright + 保存済み Google セッション Cookie を使って取得する。

使い方:
  1. 初回のみ: python3 notebooklm_extractor.py --login
     → ブラウザが立ち上がるので Google アカウントでログイン
     → 保存: data/brain/.google_cookies.json
  2. LINE で NotebookLM URL を共有 → 自動で抽出 → Wiki に取り込み

対応 URL:
  - https://notebooklm.google.com/notebook/{id}
  - https://notebooklm.google.com/notebook/{id}?tab=...
"""

import os
import re
import json
import asyncio
import logging
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)

GOOGLE_COOKIES_PATH = Path(
    os.getenv("GOOGLE_COOKIES_PATH", "/app/data/brain/.google_cookies.json")
)

NOTEBOOKLM_URL_RE = re.compile(
    r"https?://notebooklm\.google\.com/notebook/([a-zA-Z0-9_\-]+)(?:[/?#][^\s]*)?",
    re.IGNORECASE,
)


def find_notebooklm_urls(text: str) -> list[str]:
    """テキスト中の NotebookLM URL をすべて抽出"""
    if not text:
        return []
    return [m.group(0) for m in NOTEBOOKLM_URL_RE.finditer(text)]


def get_notebook_id(url: str) -> Optional[str]:
    m = NOTEBOOKLM_URL_RE.search(url)
    return m.group(1) if m else None


async def extract_notebooklm(
    url: str,
    cookies_path: Path = GOOGLE_COOKIES_PATH,
    max_chars: int = 30000,
) -> dict:
    """NotebookLM ノートから本文を抽出

    Returns:
        {
          "ok": bool,
          "text": str,
          "title": str,
          "error": str | None,
          "needs_auth": bool,    # True なら cookies セットアップを要求
        }
    """
    notebook_id = get_notebook_id(url)
    if not notebook_id:
        return {"ok": False, "text": "", "title": "", "error": "Invalid NotebookLM URL", "needs_auth": False}

    if not cookies_path.exists():
        return {
            "ok": False,
            "text": "",
            "title": "",
            "error": "Google cookies not configured. Run: python3 notebooklm_extractor.py --login",
            "needs_auth": True,
        }

    try:
        from playwright.async_api import async_playwright
    except ImportError:
        return {"ok": False, "text": "", "title": "", "error": "playwright not installed", "needs_auth": False}

    try:
        cookies = json.loads(cookies_path.read_text())
    except Exception as e:
        return {"ok": False, "text": "", "title": "", "error": f"cookies load error: {e}", "needs_auth": True}

    try:
        async with async_playwright() as p:
            browser = await p.chromium.launch(headless=True)
            context = await browser.new_context(
                user_agent="Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
            )
            await context.add_cookies(cookies)
            page = await context.new_page()
            await page.goto(url, wait_until="domcontentloaded", timeout=30000)

            # SPA なのでコンテンツ読み込み待ち
            await page.wait_for_timeout(5000)

            # ログインリダイレクトが起きた場合
            current = page.url
            if "accounts.google.com" in current or "myaccount" in current:
                await browser.close()
                return {
                    "ok": False,
                    "text": "",
                    "title": "",
                    "error": "Google login required (cookies expired)",
                    "needs_auth": True,
                }

            # タイトル取得（NotebookLM の notebook title は複数の箇所に）
            title = ""
            for selector in [
                "h1",
                "[role='heading']",
                "input[aria-label*='Notebook']",
                "[class*='notebook-title']",
            ]:
                try:
                    el = await page.query_selector(selector)
                    if el:
                        t = (await el.inner_text()).strip()
                        if t and len(t) < 200:
                            title = t
                            break
                except Exception:
                    continue
            if not title:
                title = f"NotebookLM {notebook_id[:10]}"

            # 本文（ソース + 生成ノート + チャット）を可能な限り抽出
            sections = []

            # 1. 全文テキストを fallback として取っておく
            try:
                body_text = await page.evaluate("() => document.body.innerText")
                if body_text and len(body_text) > 100:
                    sections.append(("body", body_text))
            except Exception as e:
                logger.warning(f"NotebookLM body extract error: {e}")

            # 2. 「Sources」「Notes」「Chat」のような主要ペインがあれば個別に
            for selector, label in [
                ("[aria-label*='Source']", "Sources"),
                ("[aria-label*='Note']", "Notes"),
                ("[role='main']", "Main"),
            ]:
                try:
                    els = await page.query_selector_all(selector)
                    for i, el in enumerate(els[:5]):
                        t = await el.inner_text()
                        if t and len(t) > 50:
                            sections.append((f"{label}_{i}", t))
                except Exception:
                    continue

            await browser.close()

            # 最大長のセクションを本文とする（body fallback とのトレードオフ）
            # ソース単位で取れているなら優先
            prefer = [s for s in sections if s[0] != "body"]
            chosen = prefer[0][1] if prefer else (sections[0][1] if sections else "")
            if not chosen:
                return {
                    "ok": False, "text": "", "title": title,
                    "error": "No text extracted from NotebookLM page",
                    "needs_auth": False,
                }

            if len(chosen) > max_chars:
                chosen = chosen[:max_chars] + f"\n\n...(省略: 全{len(chosen)}文字中、先頭{max_chars}のみ)"

            return {"ok": True, "text": chosen, "title": title, "error": None, "needs_auth": False}

    except Exception as e:
        logger.exception(f"NotebookLM extract error")
        return {"ok": False, "text": "", "title": "", "error": str(e), "needs_auth": False}


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# 初回ログインフロー（手動実行で Cookies 保存）
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

async def _login_flow():
    """ブラウザを開いて Google ログイン → Cookie 保存"""
    from playwright.async_api import async_playwright

    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=False)
        context = await browser.new_context()
        page = await context.new_page()
        await page.goto("https://notebooklm.google.com/", wait_until="domcontentloaded")

        print("=" * 60)
        print("ブラウザ画面で Google アカウントにログインしてください。")
        print("ログインが完了したら、このターミナルで Enter を押してください。")
        print("=" * 60)
        input()

        cookies = await context.cookies()
        GOOGLE_COOKIES_PATH.parent.mkdir(parents=True, exist_ok=True)
        GOOGLE_COOKIES_PATH.write_text(json.dumps(cookies, indent=2))
        print(f"✅ Cookies 保存完了: {GOOGLE_COOKIES_PATH}")
        print(f"   {len(cookies)} cookies")

        await browser.close()


if __name__ == "__main__":
    import sys
    if len(sys.argv) > 1 and sys.argv[1] == "--login":
        asyncio.run(_login_flow())
    else:
        print("Usage: python3 notebooklm_extractor.py --login")
