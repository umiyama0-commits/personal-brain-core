"""
owndays_mobile_date_probe.py — mobile.owndays.net の期間設定 UI を調査

目的: 過去 N 日分のデータを取得するためのメカニズムを特定する。
- 期間設定フォームの HTML 構造 (input name, 日付フォーマット)
- フォーム submit 後の URL (query param 化されるか、POST か)
- Table 3 (日商合計) に期間分の日別データが返ってくるか

出力先: data/brain/diag/date_probe/
"""
from __future__ import annotations

import asyncio
import json
import os
from datetime import date, timedelta
from pathlib import Path

# .env
_env_file = Path(__file__).parent.parent / ".env"
if _env_file.exists():
    for line in _env_file.read_text().splitlines():
        if "=" in line and not line.strip().startswith("#"):
            k, _, v = line.partition("=")
            os.environ.setdefault(k.strip(), v.strip())

BASE = "https://mobile.owndays.net"
OUT = Path(__file__).parent.parent / "data" / "brain" / "diag" / "date_probe"
OUT.mkdir(parents=True, exist_ok=True)
COOKIE_FILE = Path(__file__).parent.parent / "data" / "brain" / ".mobile_owndays_cookies.json"


async def ensure_login(page, context) -> bool:
    if COOKIE_FILE.exists():
        cookies = json.loads(COOKIE_FILE.read_text())
        await context.add_cookies(cookies)
    await page.goto(f"{BASE}/home", wait_until="domcontentloaded", timeout=30000)
    if "login" not in page.url.lower():
        return True

    user = os.environ.get("OWNDAYS_MOBILE_USER", "").strip()
    pw = os.environ.get("OWNDAYS_MOBILE_PASS", "").strip()
    if not user or not pw:
        print(f"!! no creds (user={user!r}, pw_len={len(pw)})")
        return False

    await page.goto(f"{BASE}/login", wait_until="domcontentloaded", timeout=30000)
    await page.fill('input[name="userName"]', user)
    await page.fill('input[name="password"]', pw)
    try:
        await page.check('input[name="remember"]')
    except Exception:
        pass
    await page.click('button:has-text("LOG IN"), button[type="submit"]')
    await asyncio.sleep(2)
    if "login" in page.url.lower():
        return False

    cookies = await context.cookies()
    COOKIE_FILE.write_text(json.dumps(cookies, ensure_ascii=False))
    return True


async def main():
    from playwright.async_api import async_playwright

    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True, channel="chrome")
        context = await browser.new_context()
        page = await context.new_page()

        if not await ensure_login(page, context):
            print("!! login failed")
            await browser.close()
            return

        # /total ページを開いて期間設定 UI を分析
        print(f"\n[1] GET {BASE}/total")
        await page.goto(f"{BASE}/total", wait_until="domcontentloaded", timeout=30000)
        await asyncio.sleep(2)

        html = await page.content()
        (OUT / "01_total_initial.html").write_text(html, encoding="utf-8")
        await page.screenshot(path=str(OUT / "01_total_initial.png"), full_page=True)

        # 日付設定フォームの構造を詳細分析
        forms = await page.evaluate(
            """() => {
                return Array.from(document.querySelectorAll('form')).map(f => ({
                    action: f.action,
                    method: f.method,
                    id: f.id,
                    name: f.name,
                    className: f.className,
                    inputs: Array.from(f.querySelectorAll('input, select, button, textarea')).map(i => ({
                        tag: i.tagName,
                        type: i.type || '',
                        name: i.name || '',
                        id: i.id || '',
                        class: i.className || '',
                        placeholder: i.placeholder || '',
                        value: (i.value || '').substring(0, 50),
                        min: i.min || '',
                        max: i.max || '',
                        pattern: i.pattern || '',
                        required: i.required || false,
                        readonly: i.readOnly || false,
                        text: (i.innerText || '').trim().substring(0, 50),
                        outerHTML: i.outerHTML.substring(0, 300),
                    })),
                }));
            }"""
        )
        (OUT / "02_forms.json").write_text(
            json.dumps(forms, indent=2, ensure_ascii=False), encoding="utf-8"
        )
        print("\n[2] Forms:")
        print(json.dumps(forms, indent=2, ensure_ascii=False))

        # input[type=date] / [type=text] で日付っぽいものを探す
        date_inputs = await page.evaluate(
            """() => {
                const all = Array.from(document.querySelectorAll('input'));
                return all.map(i => ({
                    tag: 'INPUT',
                    type: i.type,
                    name: i.name,
                    id: i.id,
                    class: i.className,
                    value: i.value,
                    placeholder: i.placeholder,
                    outerHTML: i.outerHTML.substring(0, 300),
                })).filter(i => i.type === 'date' || i.type === 'text' || i.name.toLowerCase().includes('date') || i.name.toLowerCase().includes('from') || i.name.toLowerCase().includes('to'));
            }"""
        )
        (OUT / "03_date_inputs.json").write_text(
            json.dumps(date_inputs, indent=2, ensure_ascii=False), encoding="utf-8"
        )
        print("\n[3] Candidate date inputs:")
        print(json.dumps(date_inputs, indent=2, ensure_ascii=False))

        # JS の網路リクエストを監視しながら submit したい。まず初期状態の Table 3 (日商合計) を見る
        t3 = await page.evaluate(
            """() => {
                const tables = Array.from(document.querySelectorAll('table'));
                if (tables.length < 3) return null;
                return Array.from(tables[2].querySelectorAll('tr')).map(tr =>
                    Array.from(tr.querySelectorAll('th, td')).map(c => c.innerText.trim())
                );
            }"""
        )
        (OUT / "04_initial_table3.json").write_text(
            json.dumps(t3, indent=2, ensure_ascii=False), encoding="utf-8"
        )
        print("\n[4] Initial Table 3 (日商合計):")
        print(json.dumps(t3, indent=2, ensure_ascii=False))

        # 期間を 7 日前〜今日に設定してみる
        today = date.today()
        start = today - timedelta(days=7)
        print(f"\n[5] Trying date range {start} → {today}")

        # 日付 input 要素 (name, id ともに不明なので placeholder/class で探す戦略)
        # forms にある input[type=text/date] を 2 つ fill
        inputs = forms[0]["inputs"] if forms else []
        text_or_date_inputs = [
            i for i in inputs if i["type"] in ("date", "text", "tel")
            and "checkbox" not in i["type"]
        ]
        print(f"   text/date inputs: {len(text_or_date_inputs)}")
        for i, inp in enumerate(text_or_date_inputs):
            print(f"   [{i}] {inp['type']:<6} name={inp['name']!r} id={inp['id']!r} class={inp['class']!r}")

        # network listener
        requests_log = []

        async def on_request(req):
            if req.method in ("POST", "GET") and any(
                kw in req.url for kw in ["total", "storelist", "area", "nation"]
            ):
                try:
                    post_data = req.post_data
                except Exception:
                    post_data = None
                requests_log.append({
                    "method": req.method,
                    "url": req.url,
                    "post_data": post_data,
                })

        page.on("request", on_request)

        # 試行 1: input[name] に fill してみる (name が判明したら優先)
        date_input_names = [inp["name"] for inp in text_or_date_inputs if inp["name"]]
        filled = False
        if len(date_input_names) >= 2:
            try:
                await page.fill(f'input[name="{date_input_names[0]}"]', start.isoformat())
                await page.fill(f'input[name="{date_input_names[1]}"]', today.isoformat())
                filled = True
                print(f"   filled via name: {date_input_names[:2]} ({start.isoformat()} / {today.isoformat()})")
            except Exception as e:
                print(f"   fill by name failed: {e}")

        if not filled and len(text_or_date_inputs) >= 2:
            # nth approach
            try:
                await page.locator(f'input[type="{text_or_date_inputs[0]["type"]}"]').nth(0).fill(start.isoformat())
                await page.locator(f'input[type="{text_or_date_inputs[1]["type"]}"]').nth(1).fill(today.isoformat())
                filled = True
                print(f"   filled via nth")
            except Exception as e:
                print(f"   fill by nth failed: {e}")

        # 決定ボタンクリック
        try:
            await page.click('button:has-text("決定"), input[type="submit"]')
            print("   clicked 決定")
        except Exception as e:
            print(f"   click failed: {e}")

        await asyncio.sleep(3)
        print(f"\n[6] After submit URL: {page.url}")

        # 結果 HTML / screenshot / body / table3
        await page.screenshot(path=str(OUT / "06_after_submit.png"), full_page=True)
        html2 = await page.content()
        (OUT / "06_after_submit.html").write_text(html2, encoding="utf-8")

        body2 = await page.evaluate("() => document.body.innerText")
        (OUT / "07_after_submit_body.txt").write_text(body2, encoding="utf-8")
        print(f"\n[7] Body (first 1500):\n{body2[:1500]}")

        # 全テーブルダンプ
        tables = await page.evaluate(
            """() => Array.from(document.querySelectorAll('table')).map(t =>
                Array.from(t.querySelectorAll('tr')).map(tr =>
                    Array.from(tr.querySelectorAll('th, td')).map(c => c.innerText.trim())
                )
            )"""
        )
        (OUT / "08_after_submit_tables.json").write_text(
            json.dumps(tables, indent=2, ensure_ascii=False), encoding="utf-8"
        )
        print(f"\n[8] Tables: {len(tables)}")
        for i, tbl in enumerate(tables):
            print(f"   Table {i+1}: {len(tbl)} rows")
            if tbl:
                print(f"     head: {tbl[0]}")
                if len(tbl) > 1:
                    print(f"     row1: {tbl[1]}")
                if len(tbl) > 5:
                    print(f"     row5: {tbl[5]}")
                if len(tbl) > 10:
                    print(f"     row10: {tbl[10]}")
                print(f"     last: {tbl[-1]}")

        # network log
        (OUT / "09_requests.json").write_text(
            json.dumps(requests_log, indent=2, ensure_ascii=False), encoding="utf-8"
        )
        print(f"\n[9] Network requests during submit ({len(requests_log)}):")
        for r in requests_log:
            print(f"   {r['method']:<5} {r['url']}")
            if r["post_data"]:
                print(f"         POST: {r['post_data'][:300]}")

        await browser.close()


if __name__ == "__main__":
    asyncio.run(main())
