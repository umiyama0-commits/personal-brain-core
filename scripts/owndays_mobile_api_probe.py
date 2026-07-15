"""
owndays_mobile_api_probe.py — API 直叩きで過去日データ取得可能か検証

SPA の API:
- POST /api/total           — {startDate, endDate} → 期間サマリ
- POST /api/totaldaily      — {startDate, endDate} → 期間内の日別総計リスト
- POST /api/storelist       — {startDate, endDate} → 期間内の店舗別合計
- POST /api/nationtotal     — {startDate, endDate}
- POST /api/areatotal       — {startDate, endDate}
- POST /api/typetotal       — {startDate, endDate}
- POST /api/leaguetotal     — {startDate, endDate}
- POST /api/storedailylist/<storeNo>  — {storeNo, startDate, endDate}

認証: Laravel Sanctum cookie (XSRF-TOKEN + laravel_session)
- axios: X-XSRF-TOKEN header (XSRF-TOKEN cookie の値)
- X-Requested-With: XMLHttpRequest
- withCredentials: true

検証:
1. 直近 7 日 /api/totaldaily で日別データが返ることを確認
2. 1年前の 7 日間 (2025-04-17 ~ 2025-04-24) でも取れるか
3. 30 日 /api/storelist で 300 店 × 30 日 の集計が返るか (重くないか)
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
OUT = Path(__file__).parent.parent / "data" / "brain" / "diag" / "api_probe"
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


async def api_post(page, endpoint: str, payload: dict):
    """axios 相当の POST をブラウザ内で実行 (Cookie/CSRF は browser context が面倒見る)"""
    js = f"""async () => {{
        const res = await axios.post('{endpoint}', {json.dumps(payload)});
        return {{status: res.status, data: res.data}};
    }}"""
    try:
        return await page.evaluate(js)
    except Exception as e:
        return {"status": -1, "error": str(e)}


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

        # /total に訪問して axios / CSRF 初期化
        await page.goto(f"{BASE}/total", wait_until="domcontentloaded", timeout=30000)
        await asyncio.sleep(1)

        today = date.today()

        tests = [
            # 1. 直近 7 日 /api/totaldaily (短期で動作確認)
            ("totaldaily_7days", "/api/totaldaily", {
                "startDate": (today - timedelta(days=7)).isoformat(),
                "endDate": today.isoformat(),
            }),
            # 2. 1 年前の 7 日間 /api/totaldaily (過去日で取れるか)
            ("totaldaily_1yr_ago_7days", "/api/totaldaily", {
                "startDate": (today - timedelta(days=365 + 7)).isoformat(),
                "endDate": (today - timedelta(days=365)).isoformat(),
            }),
            # 3. 3 年前の 7 日間 (本番想定)
            ("totaldaily_3yr_ago_7days", "/api/totaldaily", {
                "startDate": (today - timedelta(days=3 * 365 + 7)).isoformat(),
                "endDate": (today - timedelta(days=3 * 365)).isoformat(),
            }),
            # 4. 1 年分 /api/totaldaily (1 call で 365 行返るか)
            ("totaldaily_365days", "/api/totaldaily", {
                "startDate": (today - timedelta(days=365)).isoformat(),
                "endDate": today.isoformat(),
            }),
            # 5. 3 年分 /api/totaldaily (試しに上限どうか)
            ("totaldaily_1095days", "/api/totaldaily", {
                "startDate": (today - timedelta(days=1095)).isoformat(),
                "endDate": today.isoformat(),
            }),
            # 6. 1 年分 /api/storelist (全店集計 1 年)
            ("storelist_365days", "/api/storelist", {
                "startDate": (today - timedelta(days=365)).isoformat(),
                "endDate": today.isoformat(),
            }),
            # 7. 特定店舗 1 年分 日別 (川崎ダイス storeNo は要調査なので全店単位でテスト)
        ]

        for name, endpoint, payload in tests:
            print(f"\n=== {name}: POST {endpoint} {payload}")
            result = await api_post(page, endpoint, payload)
            status = result.get("status")
            data = result.get("data")
            err = result.get("error")
            print(f"   status: {status}")
            if err:
                print(f"   error: {err[:300]}")
            if isinstance(data, list):
                print(f"   → list {len(data)} items")
                if data:
                    print(f"   first: {json.dumps(data[0], ensure_ascii=False)[:300]}")
                    if len(data) > 1:
                        print(f"   last:  {json.dumps(data[-1], ensure_ascii=False)[:300]}")
            elif isinstance(data, dict):
                print(f"   → dict keys: {list(data.keys())[:15]}")
                print(f"   sample: {json.dumps(data, ensure_ascii=False)[:400]}")
            else:
                print(f"   raw: {str(data)[:500]}")

            # 保存
            (OUT / f"{name}.json").write_text(
                json.dumps(
                    {"endpoint": endpoint, "payload": payload, "result": result},
                    indent=2,
                    ensure_ascii=False,
                ),
                encoding="utf-8",
            )

        await browser.close()


if __name__ == "__main__":
    asyncio.run(main())
