"""
claude_scraper.py — Claude.ai 会話スクレイピング

Playwrightでclaude.aiにアクセスし、会話履歴を取得して
BrainWikiに取り込む（data/brain/import/ → ファイルウォッチャー経由）。

スマホ・他デバイスの会話もWebに同期されるため、
ローカルPCから定期実行すれば全会話をカバーできる。

使い方:
  python3 claude_scraper.py                # 最新10会話を取得
  python3 claude_scraper.py --count 20     # 最新20会話
  python3 claude_scraper.py --dry-run      # プレビューのみ
  python3 claude_scraper.py --no-headless  # ブラウザ表示（初回ログイン用）
"""

import asyncio
import json
import logging
import argparse
from datetime import datetime, date
from pathlib import Path

logger = logging.getLogger(__name__)

OUTPUT_DIR = Path("/Users/brain/brain-agent/data/brain/import")
PROFILE_DIR = Path("/Users/brain/brain-agent/data/brain/.scraper-profiles/claude")
STATE_FILE = Path("/Users/brain/brain-agent/data/brain/.claude_scrape_state.json")

CLAUDE_URL = "https://claude.ai"


def load_state() -> dict:
    """前回取得済みの会話IDを読み込む"""
    if STATE_FILE.exists():
        return json.loads(STATE_FILE.read_text())
    return {"scraped_ids": [], "last_run": ""}


def save_state(state: dict):
    state["last_run"] = datetime.now().isoformat()
    STATE_FILE.write_text(json.dumps(state, ensure_ascii=False, indent=2))


async def scrape_claude(
    max_conversations: int = 10,
    dry_run: bool = False,
    headless: bool = True,
):
    """claude.ai から会話履歴をスクレイプ"""
    try:
        from playwright.async_api import async_playwright
    except ImportError:
        logger.error("playwright not installed. Run: pip install playwright && playwright install chromium")
        return

    state = load_state()
    scraped_ids = set(state.get("scraped_ids", []))

    async with async_playwright() as p:
        # 専用プロファイルで persistent context を使用
        # cron実行時はウィンドウを画面外に配置して非表示
        PROFILE_DIR.mkdir(parents=True, exist_ok=True)
        args = ["--disable-blink-features=AutomationControlled"]
        if headless:
            args.append("--window-position=-2400,-2400")
        context = await p.chromium.launch_persistent_context(
            str(PROFILE_DIR),
            headless=False,  # headed必須（ボット検知回避）
            channel="chrome",
            viewport={"width": 1280, "height": 900},
            args=args,
        )

        page = context.pages[0] if context.pages else await context.new_page()
        await page.goto(CLAUDE_URL, wait_until="domcontentloaded", timeout=60000)
        await asyncio.sleep(3)

        # ログイン確認
        needs_login = "login" in page.url.lower() or "/auth" in page.url.lower()
        if not needs_login:
            # ログインボタンの有無で二重チェック
            needs_login = await page.evaluate("""() => {
                const btns = document.querySelectorAll('button');
                return Array.from(btns).some(b =>
                    b.textContent?.includes('ログイン') || b.textContent?.includes('Log in'));
            }""")

        if needs_login:
            if headless:
                await context.close()
                logger.error("ログインが必要です。--no-headless で初回実行してください。")
                return
            logger.info("ブラウザでClaude.aiにログインしてください...")
            # ログインボタンをクリック
            await page.evaluate("""() => {
                const btns = document.querySelectorAll('button, a');
                for (const b of btns) {
                    const text = b.textContent || '';
                    if (text.includes('ログイン') || text.includes('Log in') || text.includes('Sign in')) {
                        b.click();
                        break;
                    }
                }
            }""")
            # ログイン完了を待つ（/new や /chat/ に遷移）
            for _ in range(60):  # 最大3分
                await asyncio.sleep(3)
                try:
                    url = page.url
                    if "claude.ai" in url and "login" not in url and "auth" not in url:
                        await asyncio.sleep(5)
                        break
                except Exception:
                    pass
            logger.info("ログイン完了")

        # サイドバーから会話リストを取得
        logger.info("会話リストを取得中...")
        await asyncio.sleep(2)

        conversations = await page.evaluate("""() => {
            const items = [];
            // サイドバーの会話リンクを取得（nav内外どちらでも）
            const links = document.querySelectorAll('a[href*="/chat/"]');
            for (const a of links) {
                const href = a.getAttribute('href') || '';
                const match = href.match(/\\/chat\\/([a-f0-9-]+)/);
                if (match) {
                    items.push({
                        id: match[1],
                        title: a.textContent?.trim() || 'Untitled',
                        href: href,
                    });
                }
            }
            return items;
        }""")

        if not conversations:
            logger.warning("会話が見つかりません。DOMセレクタの更新が必要かもしれません。")
            await context.close()
            return

        # 未取得の会話をフィルタ + 上限
        new_convos = [c for c in conversations if c["id"] not in scraped_ids]
        target = new_convos[:max_conversations]
        logger.info(f"全{len(conversations)}会話, 未取得{len(new_convos)}件, 今回{len(target)}件処理")

        all_exports = []
        new_ids = []

        for i, convo in enumerate(target):
            title = convo["title"]
            convo_id = convo["id"]
            logger.info(f"[{i+1}/{len(target)}] {title}")

            # 会話ページに遷移
            await page.goto(f"{CLAUDE_URL}{convo['href']}", wait_until="domcontentloaded", timeout=30000)
            await asyncio.sleep(2)

            # スクロールして全メッセージを読み込む
            prev_count = 0
            for _ in range(10):  # 最大10回スクロール
                msg_count = await page.evaluate("""() => {
                    const msgs = document.querySelectorAll(
                        '[data-testid="user-message"], [data-testid="assistant-message"], ' +
                        '.font-user-message, .font-claude-message, ' +
                        '[data-role="user"], [data-role="assistant"]'
                    );
                    return msgs.length;
                }""")
                if msg_count == prev_count:
                    break
                prev_count = msg_count
                await page.evaluate("window.scrollTo(0, 0)")
                await asyncio.sleep(1)

            # メッセージ抽出
            messages = await page.evaluate("""() => {
                const results = [];

                // 方法1: data-testid ベース
                const turns = document.querySelectorAll(
                    '[data-testid="user-message"], [data-testid="assistant-message"]'
                );
                if (turns.length > 0) {
                    for (const el of turns) {
                        const testId = el.getAttribute('data-testid') || '';
                        const role = testId.includes('user') ? 'user' : 'assistant';
                        const text = el.innerText?.trim() || '';
                        if (text) results.push({ role, content: text });
                    }
                    return results;
                }

                // 方法2: data-role ベース
                const roleEls = document.querySelectorAll('[data-role="user"], [data-role="assistant"]');
                if (roleEls.length > 0) {
                    for (const el of roleEls) {
                        const role = el.getAttribute('data-role');
                        const text = el.innerText?.trim() || '';
                        if (text) results.push({ role, content: text });
                    }
                    return results;
                }

                // 方法3: クラス名ベース
                const userEls = document.querySelectorAll('.font-user-message');
                const assistantEls = document.querySelectorAll('.font-claude-message');
                const all = [
                    ...Array.from(userEls).map(el => ({ el, role: 'user' })),
                    ...Array.from(assistantEls).map(el => ({ el, role: 'assistant' })),
                ].sort((a, b) => {
                    const pos = a.el.compareDocumentPosition(b.el);
                    return pos & Node.DOCUMENT_POSITION_FOLLOWING ? -1 : 1;
                });
                for (const { el, role } of all) {
                    const text = el.innerText?.trim() || '';
                    if (text) results.push({ role, content: text });
                }

                // 方法4: フォールバック — メインエリア全体
                if (results.length === 0) {
                    const main = document.querySelector('main, [role="main"]');
                    if (main) {
                        results.push({ role: 'unknown', content: main.innerText?.trim() || '' });
                    }
                }

                return results;
            }""")

            if messages:
                all_exports.append({
                    "id": convo_id,
                    "title": title,
                    "messages": messages,
                    "scraped_at": datetime.now().isoformat(),
                })
                new_ids.append(convo_id)
                logger.info(f"  → {len(messages)} メッセージ取得")
            else:
                logger.warning(f"  → メッセージ取得失敗")

        await context.close()

        # ─── 出力 ───
        if dry_run:
            for export in all_exports:
                print(f"\n{'='*50}")
                print(f"[{export['title']}] ({len(export['messages'])} msgs)")
                for m in export["messages"][:4]:
                    role = "User" if m["role"] == "user" else "Claude"
                    print(f"  [{role}] {m['content'][:100]}")
                if len(export["messages"]) > 4:
                    print(f"  ... +{len(export['messages'])-4} more")
            return all_exports

        # import/ に保存
        OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
        today = date.today().isoformat()

        for export in all_exports:
            slug = export["title"].replace("/", "_").replace(" ", "_")[:40]
            safe_slug = "".join(c for c in slug if c.isalnum() or c in "_-")
            filename = f"claude_{safe_slug}_{today}.txt"
            filepath = OUTPUT_DIR / filename

            lines = [f"[Claude.ai] {export['title']}", today, ""]
            for m in export["messages"]:
                sender = "海山丈司" if m["role"] == "user" else "Claude"
                lines.append(f"\t{sender}\t{m['content']}")
            lines.append("")

            filepath.write_text("\n".join(lines), encoding="utf-8")
            logger.info(f"  保存: {filename}")

        # ステート更新
        scraped_ids.update(new_ids)
        state["scraped_ids"] = list(scraped_ids)[-500:]  # 直近500件保持
        save_state(state)

        logger.info(f"完了: {len(all_exports)} 会話 → data/brain/import/")
        return all_exports


async def main():
    parser = argparse.ArgumentParser(description="Claude.ai 会話スクレイパー")
    parser.add_argument("--count", type=int, default=10, help="取得する会話数")
    parser.add_argument("--dry-run", action="store_true", help="取り込みせずにプレビュー")
    parser.add_argument("--no-headless", action="store_true", help="ブラウザを表示する（初回ログイン用）")
    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s")
    await scrape_claude(
        max_conversations=args.count,
        dry_run=args.dry_run,
        headless=not args.no_headless,
    )


if __name__ == "__main__":
    asyncio.run(main())
