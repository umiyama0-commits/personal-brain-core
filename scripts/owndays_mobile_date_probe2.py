"""
owndays_mobile_date_probe2.py — 決定ボタンの実体と submit 動作を特定

probe1 で判明:
- input[type=date, name=startDate/endDate] が存在するが form の外
- button:has-text("決定") は見つからない
- 「決定」テキストは body 内にある

今回調査:
- 決定クリック対象の要素 (a, div, span, button) を特定
- クリック時のネットワーク (XHR/fetch)
- 日付変更 + 決定 → 結果の Table 3 で過去日データが取れるか
"""
from __future__ import annotations

import asyncio
import json
import os
from datetime import date, timedelta
from pathlib import Path

_env_file = Path(__file__).parent.parent / ".env"
if _env_file.exists():
    for line in _env_file.read_text().splitlines():
        if "=" in line and not line.strip().startswith("#"):
            k, _, v = line.partition("=")
            os.environ.setdefault(k.strip(), v.strip())

BASE = "https://mobile.owndays.net"
OUT = Path(__file__).parent.parent / "data" / "brain" / "diag" / "date_probe2"
OUT.mkdir(parents=True, exist_ok=True)
COOKIE_FILE = Path(__file__).parent.parent / "data" / "brain" / ".mobile_owndays_cookies.json"


async def ensure_login(page, context):
    if COOKIE_FILE.exists():
        await context.add_cookies(json.loads(COOKIE_FILE.read_text()))
    await page.goto(f"{BASE}/home", wait_until="domcontentloaded", timeout=30000)
    if "login" not in page.url.lower():
        return True
    user = os.environ.get("OWNDAYS_MOBILE_USER", "").strip()
    pw = os.environ.get("OWNDAYS_MOBILE_PASS", "").strip()
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
    COOKIE_FILE.write_text(json.dumps(await context.cookies(), ensure_ascii=False))
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

        await page.goto(f"{BASE}/total", wait_until="domcontentloaded", timeout=30000)
        await asyncio.sleep(2)

        # 決定ボタン候補を網羅的に探す
        decision = await page.evaluate(
            """() => {
                const all = Array.from(document.querySelectorAll('*'));
                const cands = all.filter(el => {
                    const t = (el.innerText || '').trim();
                    return t === '決定' || t.endsWith('決定');
                });
                return cands.slice(0, 20).map(el => ({
                    tag: el.tagName,
                    id: el.id,
                    class: el.className,
                    text: (el.innerText || '').trim().substring(0, 40),
                    onclick: el.getAttribute('onclick') || '',
                    href: el.getAttribute('href') || '',
                    outerHTML: el.outerHTML.substring(0, 400),
                }));
            }"""
        )
        (OUT / "01_decision_candidates.json").write_text(
            json.dumps(decision, indent=2, ensure_ascii=False), encoding="utf-8"
        )
        print(f"\n[1] 決定 candidates ({len(decision)}):")
        for c in decision:
            print(f"   <{c['tag'].lower()} id={c['id']!r} class={c['class']!r}>")
            print(f"      text: {c['text']!r}")
            if c["onclick"]:
                print(f"      onclick: {c['onclick']}")
            if c["href"]:
                print(f"      href: {c['href']}")
            print(f"      outerHTML: {c['outerHTML'][:200]}")

        # JavaScript の関数シグネチャ / イベントリスナ
        # window に定義されたキー関数を列挙
        window_keys = await page.evaluate(
            """() => Object.keys(window).filter(k =>
                typeof window[k] === 'function' &&
                !k.startsWith('webkit') &&
                !k.startsWith('on') &&
                k.length < 40
            ).slice(0, 60)"""
        )
        print(f"\n[2] window.* functions: {window_keys}")

        # request/response listener
        requests_log = []

        async def on_request(req):
            requests_log.append({
                "phase": "request",
                "method": req.method,
                "url": req.url,
                "post_data": (req.post_data or "")[:500],
                "headers": dict(req.headers),
            })

        async def on_response(res):
            try:
                requests_log.append({
                    "phase": "response",
                    "status": res.status,
                    "url": res.url,
                    "content_type": res.headers.get("content-type", ""),
                })
            except Exception:
                pass

        page.on("request", lambda r: on_request_sync(r))
        page.on("response", lambda r: on_response_sync(r))

        def on_request_sync(req):
            try:
                requests_log.append({
                    "phase": "request",
                    "method": req.method,
                    "url": req.url,
                    "post_data": (req.post_data or "")[:500],
                })
            except Exception:
                pass

        def on_response_sync(res):
            try:
                requests_log.append({
                    "phase": "response",
                    "status": res.status,
                    "url": res.url,
                    "content_type": res.headers.get("content-type", "")[:80],
                })
            except Exception:
                pass

        page.on("request", on_request_sync)
        page.on("response", on_response_sync)

        # 日付変更 + submit を試す
        today = date.today()
        start = today - timedelta(days=30)

        print(f"\n[3] Set startDate={start.isoformat()} / endDate={today.isoformat()}")

        # input[type=date] は直接 fill できない時があるのでまず JS で value set + event dispatch
        await page.evaluate(
            f"""() => {{
                const s = document.querySelector('input[name=startDate]');
                const e = document.querySelector('input[name=endDate]');
                if (s) {{ s.value = '{start.isoformat()}'; s.dispatchEvent(new Event('change', {{bubbles:true}})); s.dispatchEvent(new Event('input', {{bubbles:true}})); }}
                if (e) {{ e.value = '{today.isoformat()}'; e.dispatchEvent(new Event('change', {{bubbles:true}})); e.dispatchEvent(new Event('input', {{bubbles:true}})); }}
            }}"""
        )
        vs = await page.evaluate(
            """() => ({
                startDate: document.querySelector('input[name=startDate]')?.value,
                endDate: document.querySelector('input[name=endDate]')?.value,
            })"""
        )
        print(f"   values after set: {vs}")

        # 決定 要素をクリックする (candidates の最初の結果を優先)
        if decision:
            print(f"\n[4] click first 決定 candidate: <{decision[0]['tag']} id={decision[0]['id']!r}>")
            try:
                selector = ""
                if decision[0]["id"]:
                    selector = f'#{decision[0]["id"]}'
                elif decision[0]["tag"].lower() in ("a", "button"):
                    selector = f'{decision[0]["tag"].lower()}:has-text("決定")'
                else:
                    selector = f'{decision[0]["tag"].lower()}:has-text("決定")'
                print(f"   selector: {selector}")
                await page.click(selector)
                print("   clicked")
            except Exception as e:
                print(f"   click failed: {e}")
                # fallback: JS で click()
                try:
                    tag = decision[0]["tag"].lower()
                    await page.evaluate(
                        f"""() => {{
                            const cands = Array.from(document.querySelectorAll('{tag}'));
                            for (const c of cands) {{
                                if ((c.innerText || '').trim() === '決定') {{
                                    c.click();
                                    return true;
                                }}
                            }}
                            return false;
                        }}"""
                    )
                    print("   JS click triggered")
                except Exception as e2:
                    print(f"   JS click also failed: {e2}")

        await asyncio.sleep(5)

        # submit 後の状態
        print(f"\n[5] After submit URL: {page.url}")
        html = await page.content()
        (OUT / "02_after.html").write_text(html, encoding="utf-8")
        body = await page.evaluate("() => document.body.innerText")
        (OUT / "03_after_body.txt").write_text(body, encoding="utf-8")
        print(f"\n[5] Body (first 2000):\n{body[:2000]}")

        # Table 3 (日商合計) を確認
        tables = await page.evaluate(
            """() => Array.from(document.querySelectorAll('table')).map(t =>
                Array.from(t.querySelectorAll('tr')).map(tr =>
                    Array.from(tr.querySelectorAll('th, td')).map(c => c.innerText.trim())
                )
            )"""
        )
        (OUT / "04_tables.json").write_text(
            json.dumps(tables, indent=2, ensure_ascii=False), encoding="utf-8"
        )

        print(f"\n[6] Tables: {len(tables)}")
        for i, tbl in enumerate(tables):
            print(f"   Table {i+1}: {len(tbl)} rows")
            if i == 2 or len(tbl) > 5:  # Table 3 detail
                for r in tbl[:35]:
                    print(f"     {r}")
                if len(tbl) > 35:
                    print(f"     ... ({len(tbl) - 35} more rows)")

        # network
        (OUT / "05_requests.json").write_text(
            json.dumps(requests_log, indent=2, ensure_ascii=False), encoding="utf-8"
        )
        filtered = [
            r for r in requests_log
            if r.get("url", "").startswith(BASE)
            or "total" in r.get("url", "").lower()
        ]
        print(f"\n[7] Requests ({len(filtered)}):")
        for r in filtered[-20:]:
            if r["phase"] == "request":
                print(f"   REQ {r['method']:<5} {r['url']}")
                if r.get("post_data"):
                    print(f"        POST: {r['post_data'][:200]}")
            else:
                print(f"   RES {r.get('status')} {r['url']}")

        await browser.close()


if __name__ == "__main__":
    asyncio.run(main())
