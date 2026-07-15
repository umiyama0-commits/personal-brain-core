"""
owndays_mobile_login_diag.py — mobile.owndays.net ログイン失敗の原因を調べるための一度きり診断

- ログインページの HTML 構造を dump
- フォームフィールドと hidden input (CSRF token 等) を全て列挙
- 入力→クリック→結果ページを screenshot + HTML dump
- data/brain/diag/ に保存
"""
from __future__ import annotations

import asyncio
import os
from pathlib import Path

# .env 読み込み
_env_file = Path(__file__).parent.parent / ".env"
if _env_file.exists():
    for line in _env_file.read_text().splitlines():
        if "=" in line and not line.strip().startswith("#"):
            k, _, v = line.partition("=")
            os.environ.setdefault(k.strip(), v.strip())

BASE = "https://mobile.owndays.net"
LOGIN = f"{BASE}/login"
OUT = Path(__file__).parent.parent / "data" / "brain" / "diag"
OUT.mkdir(parents=True, exist_ok=True)


async def main():
    from playwright.async_api import async_playwright

    user = os.environ.get("OWNDAYS_MOBILE_USER", "").strip()
    pw = os.environ.get("OWNDAYS_MOBILE_PASS", "").strip()
    print(f"creds: user={user!r} pw_len={len(pw)}")

    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True, channel="chrome")
        context = await browser.new_context()
        page = await context.new_page()

        # 1) ログインページ GET
        print(f"\n[1] GET {LOGIN}")
        await page.goto(LOGIN, wait_until="domcontentloaded", timeout=30000)
        await asyncio.sleep(1)

        html_before = await page.content()
        (OUT / "01_login_page.html").write_text(html_before, encoding="utf-8")
        await page.screenshot(path=str(OUT / "01_login_page.png"), full_page=True)

        # 2) フォーム構造 dump
        form_info = await page.evaluate(
            """() => {
                const forms = Array.from(document.querySelectorAll('form'));
                return forms.map(f => ({
                    action: f.action,
                    method: f.method,
                    inputs: Array.from(f.querySelectorAll('input, select, button, textarea')).map(i => ({
                        tag: i.tagName,
                        type: i.type || '',
                        name: i.name || '',
                        id: i.id || '',
                        placeholder: i.placeholder || '',
                        value: i.type === 'password' ? '[hidden]' : (i.value || '').substring(0, 50),
                        required: i.required || false,
                        text: (i.innerText || '').trim().substring(0, 50),
                    })),
                }));
            }"""
        )
        print("\n[2] Form structure:")
        import json
        print(json.dumps(form_info, indent=2, ensure_ascii=False))
        (OUT / "02_form_structure.json").write_text(
            json.dumps(form_info, indent=2, ensure_ascii=False), encoding="utf-8"
        )

        # 3) ページテキスト全体
        body_text = await page.evaluate("() => document.body.innerText")
        print("\n[3] Body text (first 800 chars):")
        print(body_text[:800])
        (OUT / "03_body_text.txt").write_text(body_text, encoding="utf-8")

        # 4) ログイン試行 (詳細 log つき)
        print(f"\n[4] fill userName={user!r}")
        try:
            await page.fill('input[name="userName"]', user)
            filled_u = await page.eval_on_selector(
                'input[name="userName"]', "el => el.value"
            )
            print(f"   filled value = {filled_u!r}")
        except Exception as e:
            print(f"   ERROR: {e}")

        print(f"[4] fill password (len={len(pw)})")
        try:
            await page.fill('input[name="password"]', pw)
            filled_p_len = await page.eval_on_selector(
                'input[name="password"]', "el => el.value.length"
            )
            print(f"   filled password length = {filled_p_len}")
        except Exception as e:
            print(f"   ERROR: {e}")

        # remember checkbox あれば check
        try:
            cb = await page.query_selector('input[name="remember"]')
            if cb:
                await page.check('input[name="remember"]')
                print("   remember: checked")
        except Exception:
            pass

        await page.screenshot(path=str(OUT / "04_before_submit.png"), full_page=True)

        # 5) submit
        print("\n[5] click LOG IN button")
        try:
            await page.click('button:has-text("LOG IN"), button[type="submit"]')
        except Exception as e:
            print(f"   ERROR: {e}")

        await asyncio.sleep(3)
        await page.screenshot(path=str(OUT / "05_after_submit.png"), full_page=True)
        html_after = await page.content()
        (OUT / "05_after_submit.html").write_text(html_after, encoding="utf-8")
        body_after = await page.evaluate("() => document.body.innerText")
        print(f"\n[5] After submit — URL: {page.url}")
        print(f"[5] Body text (first 600 chars):")
        print(body_after[:600])
        (OUT / "05_after_submit.txt").write_text(body_after, encoding="utf-8")

        # 6) network log — last POST response
        print("\n[6] See /Users/brain/brain-agent/data/brain/diag/ for all files")

        await browser.close()


if __name__ == "__main__":
    asyncio.run(main())
