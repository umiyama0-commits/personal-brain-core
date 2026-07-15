"""
stapa_scraper.py — STAPA OWNDAYS MAGAZINE スクレイパー

「海山タケシ社長のもぐもぐダイアリー」を含む全オンマガ記事を取得し、
data/brain/import/ に配置してBrainWikiに自動取り込み。

使い方:
  python3 stapa_scraper.py                # 未取得の新着記事のみ
  python3 stapa_scraper.py --all          # 全記事を再取得
  python3 stapa_scraper.py --dry-run      # プレビューのみ
"""

# ★2026-07-12 loud-fail 23連敗の真因 fix: cron の python は system 3.9 (PATH 最小) で、
# 7/11 [1f0f4e2] が足した `Path | None` (PEP604 = 3.10+) が import 即死していた。
# lazy annotation 化で 3.9 でも安全に (chain の magazine_backfill/persona_ingest と同じ流儀)。
from __future__ import annotations

import asyncio
import json
import logging
import argparse
import os
from datetime import date
from pathlib import Path

logger = logging.getLogger(__name__)

STAPA_BASE = "https://stapa.owndays.net"
STAPA_LOGIN = f"{STAPA_BASE}/login"
STAPA_MAGAZINE = f"{STAPA_BASE}/owndays-magazine-details"
OUTPUT_DIR = Path("/Users/brain/brain-agent/data/brain/import")
STATE_FILE = Path("/Users/brain/brain-agent/data/brain/.stapa_state.json")
COOKIE_FILE = Path("/Users/brain/brain-agent/data/brain/.stapa_cookies.json")

# ★平文 hardcode 禁止 (2026-05-23 LEE レビュー §3.1): env 経由のみ
LOGIN_USER = os.getenv("STAPA_USER", "")
LOGIN_PASS = os.getenv("STAPA_PASS", "")
if not LOGIN_USER or not LOGIN_PASS:
    import sys as _sys
    print("[stapa_scraper] STAPA_USER / STAPA_PASS が .env に未設定", file=_sys.stderr)


def load_state() -> dict:
    if STATE_FILE.exists():
        return json.loads(STATE_FILE.read_text())
    return {"last_id": 0}


def save_state(state: dict):
    STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
    STATE_FILE.write_text(json.dumps(state))


async def scrape_stapa(fetch_all: bool = False, dry_run: bool = False,
                       output_dir: Path | None = None):
    try:
        from playwright.async_api import async_playwright
    except ImportError:
        logger.error("playwright not installed")
        return

    state = load_state()
    start_id = 1 if fetch_all else state.get("last_id", 0) + 1

    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True, channel="chrome")
        context = await browser.new_context()

        # Cookie復元
        if COOKIE_FILE.exists():
            cookies = json.loads(COOKIE_FILE.read_text())
            await context.add_cookies(cookies)

        page = await context.new_page()

        # ログイン確認
        await page.goto(f"{STAPA_BASE}/main", wait_until="domcontentloaded", timeout=30000)
        if "login" in page.url.lower():
            logger.info("STAPAにログイン中...")
            await page.fill('input[name="employee_no"], input[type="text"]', LOGIN_USER)
            await page.fill('input[name="password"], input[type="password"]', LOGIN_PASS)
            await page.click('button[type="submit"], input[type="submit"], button:has-text("ログイン")')
            await page.wait_for_url(f"**{STAPA_BASE}/**", timeout=15000)
            cookies = await context.cookies()
            COOKIE_FILE.parent.mkdir(parents=True, exist_ok=True)
            COOKIE_FILE.write_text(json.dumps(cookies))
            logger.info("ログイン成功")

        # 最新のマガジンIDを取得
        await page.goto(f"{STAPA_BASE}/owndays-magazine", wait_until="domcontentloaded", timeout=30000)
        max_id = await page.evaluate("""() => {
            const links = document.querySelectorAll('a[href*="magazine-details/"]');
            let maxId = 0;
            for (const a of links) {
                const m = a.href.match(/details\\/(\\d+)/);
                if (m) maxId = Math.max(maxId, parseInt(m[1]));
            }
            return maxId;
        }""")

        if max_id == 0:
            logger.warning("マガジンIDが取得できませんでした")
            await browser.close()
            return

        logger.info(f"マガジンID範囲: {start_id} → {max_id}")

        articles = []
        max_fetched_id = state.get("last_id", 0)

        for article_id in range(start_id, max_id + 1):
            url = f"{STAPA_MAGAZINE}/{article_id}"
            try:
                await page.goto(url, wait_until="domcontentloaded", timeout=20000)
                await asyncio.sleep(1)

                data = await page.evaluate("""() => {
                    const body = document.body.innerText;
                    // タイトル取得
                    const titleMatch = body.match(/Vol\\.\\d+/);
                    const title = titleMatch ? titleMatch[0] : '';
                    // もぐもぐダイアリー部分を抽出
                    // ★2026-07-05 bug fix: マーカーは「目次(■一覧)」と「本文見出し」の
                    // 2 箇所に出る。従来の indexOf は先に来る目次を掴み、本文でなく
                    // 目次の断片 (次の項目名だけ) を保存していた (mogumog_*.md が数百byte)。
                    // → 全出現箇所について「次の ■ セクションまで」を切り出し、最長 = 本文を採用。
                    const marker = '海山タケシ社長のもぐもぐダイアリー';
                    let moguText = '';
                    let from = 0, idx;
                    while ((idx = body.indexOf(marker, from)) > -1) {
                        const seg = body.substring(idx + marker.length).split('■')[0];
                        if (seg.length > moguText.length) moguText = seg;
                        from = idx + marker.length;
                    }
                    moguText = moguText.trim();
                    return {
                        title: title,
                        fullText: body.substring(0, 15000),
                        moguText: moguText.substring(0, 5000),
                        url: window.location.href
                    };
                }""")

                if data["title"]:
                    articles.append({
                        "id": article_id,
                        "title": data["title"],
                        "moguText": data["moguText"],
                        "fullText": data["fullText"],
                        "url": data["url"],
                    })
                    logger.info(f"  [{article_id}] {data['title']} — もぐもぐ: {len(data['moguText'])} chars")
                    max_fetched_id = max(max_fetched_id, article_id)

            except Exception as e:
                logger.warning(f"  [{article_id}] スキップ: {e}")

        await browser.close()

        logger.info(f"取得完了: {len(articles)} 記事")

        if dry_run:
            for a in articles[-5:]:
                print(f"\n{'='*50}")
                print(f"[{a['id']}] {a['title']}")
                if a["moguText"]:
                    print(f"もぐもぐダイアリー: {a['moguText'][:200]}...")
            return articles

        # 保存
        # ★2026-07-06 backfill: output_dir 指定時は IMPORT_DIR を経由しない (= watcher の
        # LLM compile に乗せず、magazine_persona_ingest が直接読む side-dir へ書く。
        # --all の数百 file を import 経路へ一括投下すると通常取込が数時間詰まるため)
        out_dir = output_dir or OUTPUT_DIR
        out_dir.mkdir(parents=True, exist_ok=True)
        today = date.today().isoformat()

        # もぐもぐダイアリーを個別ファイルで保存
        for a in articles:
            if a["moguText"]:
                slug = a["title"].replace(".", "_")
                filename = f"mogumog_{slug}_id{a['id']}_{today}.txt"
                filepath = out_dir / filename
                content = f"[OWNDAYS MAGAZINE {a['title']}]\n"
                content += f"URL: {a['url']}\n\n"
                content += f"■海山タケシ社長のもぐもぐダイアリー\n\n"
                content += a["moguText"]
                filepath.write_text(content, encoding="utf-8")
                logger.info(f"  保存: {filepath.name}")

        # 全記事もバッチで保存（他のコーナーも学習用）
        batch_size = 5
        for i in range(0, len(articles), batch_size):
            batch = articles[i:i + batch_size]
            filename = f"onmaga_batch_{batch[0]['id']}-{batch[-1]['id']}_{today}.txt"
            filepath = out_dir / filename
            lines = [f"[OWNDAYS MAGAZINE Export] {today}\n"]
            for a in batch:
                lines.append(f"## {a['title']}")
                lines.append(f"URL: {a['url']}\n")
                lines.append(a["fullText"][:8000])
                lines.append("\n---\n")
            filepath.write_text("\n".join(lines), encoding="utf-8")
            logger.info(f"  保存: {filepath.name}")

        # 状態保存
        save_state({"last_id": max_fetched_id})
        logger.info(f"完了: last_id={max_fetched_id}")

        return articles


async def main():
    parser = argparse.ArgumentParser(description="STAPA OWNDAYS MAGAZINE スクレイパー")
    parser.add_argument("--all", action="store_true", help="全記事を取得")
    parser.add_argument("--dry-run", action="store_true", help="プレビューのみ")
    parser.add_argument("--output-dir", default="",
                        help="保存先 override (backfill 用: IMPORT_DIR/compile を経由せず直接書く)")
    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s")
    await scrape_stapa(fetch_all=args.all, dry_run=args.dry_run,
                       output_dir=Path(args.output_dir) if args.output_dir else None)


if __name__ == "__main__":
    asyncio.run(main())
