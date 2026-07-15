"""
lineworks_scraper.py — LINE Works Web スクレイピングによる会話取り込み

Playwright MCP経由でLINE Works Webにアクセスし、
トークルームのメッセージを抽出してBrainWikiに取り込む。

使い方:
  python lineworks_scraper.py                 # 最新20ルーム、差分のみ取込
  python lineworks_scraper.py --rooms 5       # 最新5件のみ
  python lineworks_scraper.py --dry-run       # 取り込みせずにプレビュー
  python lineworks_scraper.py --backfill      # 全ルーム、全履歴をスクロールバックで取得
  python lineworks_scraper.py --backfill --rooms 5  # 指定数のルームをフルバックフィル

履歴取得: ルームを開いた後、メッセージエリアを上にスクロールして
古いメッセージをロードし続ける。過去に取り込んだメッセージは
.lineworks_state.jsonで追跡して重複排除。
"""

import asyncio
import fcntl
import hashlib
import json
import logging
import argparse
import os
import re
import sys
from datetime import datetime, date
from pathlib import Path

import httpx

logger = logging.getLogger(__name__)

# Playwright MCPを使わず、直接Playwrightを使用
# pip install playwright && playwright install chromium

from content_extractor import (
    extract_google_doc_via_playwright,
    extract_image_text,
    extract_file_text,
    download_file_from_chat,
)

LINEWORKS_URL = "https://talk.worksmobile.com/"
OUTPUT_DIR = Path("/Users/brain/brain-agent/data/brain/import")
COOKIE_FILE = Path("/Users/brain/brain-agent/data/brain/.lineworks_cookies.json")
STATE_FILE = Path("/Users/brain/brain-agent/data/brain/.lineworks_state.json")
LOCK_FILE = Path("/Users/brain/brain-agent/data/brain/.lineworks_scraper.lock")
# ★平文 hardcode 禁止 (2026-05-23 LEE レビュー §3.1): env 経由のみ
LITELLM_URL = os.getenv("LITELLM_URL", "http://localhost:4000")
LITELLM_KEY = os.getenv("LITELLM_MASTER_KEY", "")

# スクロールバック設定
SCROLL_MAX_ITERATIONS = 500      # セーフティ上限（1ルームあたり最大スクロール回数）
SCROLL_WAIT_MS = 700              # スクロール後、次のメッセージ群が読み込まれる待ち時間
SCROLL_STABLE_ROUNDS = 4          # 同じメッセージ数がN回続いたら「先頭到達」と判定


def _msg_hash(m: dict) -> str:
    """メッセージの一意ハッシュ（重複検出用）。"""
    # 日付・時刻・送信者・本文先頭200文字で識別
    key = "\t".join([
        str(m.get("date") or ""),
        str(m.get("time") or ""),
        str(m.get("sender") or ""),
        str(m.get("text") or "")[:200],
    ])
    return hashlib.md5(key.encode("utf-8")).hexdigest()[:16]


def _load_state() -> dict:
    """ルームごとの取込済みメッセージハッシュを読み込む。"""
    if STATE_FILE.exists():
        try:
            return json.loads(STATE_FILE.read_text(encoding="utf-8"))
        except Exception as e:
            logger.warning(f"state file read failed: {e}")
    return {}


def _save_state(state: dict) -> None:
    """ルームごとの取込済みメッセージハッシュを保存。"""
    STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
    STATE_FILE.write_text(
        json.dumps(state, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )


async def _scroll_to_top(page, max_iters: int = SCROLL_MAX_ITERATIONS) -> dict:
    """チャットビューを上にスクロールし、全ての古いメッセージを読み込む。"""
    result = await page.evaluate(
        """async ({maxIters, waitMs, stableRounds}) => {
            const sleep = (ms) => new Promise(r => setTimeout(r, ms));

            // スクロール可能なコンテナを探索
            const candidates = [];
            for (const sel of ['.chat_view', '.msg_area', '[class*="message_list"]',
                               '[class*="scroll_area"]', '[class*="chat_body"]']) {
                document.querySelectorAll(sel).forEach(el => candidates.push(el));
            }
            // 実際にスクロール可能なもの（scrollHeight > clientHeight）を優先
            let view = candidates.find(c => c && c.scrollHeight > c.clientHeight + 50);
            if (!view) {
                // フォールバック: .msg_wrap を含む最も近い祖先のスクロール可能要素
                const firstMsg = document.querySelector('.msg_wrap');
                if (firstMsg) {
                    let el = firstMsg.parentElement;
                    while (el && el !== document.body) {
                        const s = getComputedStyle(el);
                        if ((s.overflowY === 'auto' || s.overflowY === 'scroll') &&
                            el.scrollHeight > el.clientHeight + 50) {
                            view = el; break;
                        }
                        el = el.parentElement;
                    }
                }
            }
            if (!view) return {ok: false, reason: 'no scrollable view found', count: 0, iters: 0};

            let lastCount = document.querySelectorAll('.msg_wrap').length;
            let stable = 0;
            let iters = 0;

            for (let i = 0; i < maxIters; i++) {
                iters++;
                view.scrollTop = 0;
                // 時々、view.scrollTo も試す（LINE Worksの一部UIで効く）
                if (view.scrollTo) view.scrollTo({top: 0, behavior: 'instant'});
                await sleep(waitMs);
                const count = document.querySelectorAll('.msg_wrap').length;
                if (count === lastCount) {
                    stable++;
                    if (stable >= stableRounds) break;
                } else {
                    stable = 0;
                }
                lastCount = count;
            }
            return {ok: true, count: lastCount, iters};
        }""",
        {
            "maxIters": max_iters,
            "waitMs": SCROLL_WAIT_MS,
            "stableRounds": SCROLL_STABLE_ROUNDS,
        },
    )
    return result


async def scrape_lineworks(
    max_rooms: int = 10,
    dry_run: bool = False,
    headless: bool = True,
    login_id: str = "",
    login_pw: str = "",
    backfill: bool = False,
):
    """LINE Works Webからトークルームのメッセージをスクレイプ"""
    try:
        from playwright.async_api import async_playwright
    except ImportError:
        logger.error("playwright not installed. Run: pip install playwright && playwright install chromium")
        return

    async with async_playwright() as p:
        # ── ★2026-06-08 既読ブロック根治 (本命): noti ホストを DNS レベルで遮断 ──
        # 既読通知の MQTT (wss://*-noti.worksmobile.com/wmqtt) は SharedWorker 内で動き、
        # page-scope の WebSocket patch / CDP / route が届かなかった (= テストで遮断 0 件確認)。
        # Chrome 起動フラグ --host-resolver-rules で noti サブドメインの名前解決を全 renderer
        # (worker 含む) で失敗させ、wmqtt 接続自体を成立させない。talk/auth/www.worksmobile.com
        # は対象外なので login / chat 表示には影響しない (= noti ラベルを含むホストのみ NOTFOUND)。
        browser = await p.chromium.launch(
            headless=headless,
            channel="chrome",
            args=[
                "--host-resolver-rules=MAP *noti.worksmobile.com ^NOTFOUND,"
                "MAP *-noti.worksmobile.com ^NOTFOUND",
            ],
        )
        context = await browser.new_context()

        # 保存済みCookieがあれば読み込み
        if COOKIE_FILE.exists():
            cookies = json.loads(COOKIE_FILE.read_text())
            await context.add_cookies(cookies)

        # ── ★2026-06-08 既読ブロック根治 (2/2): context.route で noti/wmqtt を遮断 ──
        # context.route は page だけでなく worker の network request も対象になるため、
        # CDP page-scope block を補完して SharedWorker の wmqtt 接続も塞ぐ。
        # WS handshake (HTTP upgrade) / worker script fetch のどちらで来ても abort。
        _noti_blocked = {"n": 0}
        async def _block_noti(route):
            try:
                u = route.request.url.lower()
                if "noti.worksmobile.com" in u or "wmqtt" in u:
                    _noti_blocked["n"] += 1
                    await route.abort()
                    return
                await route.continue_()
            except Exception:
                try:
                    await route.continue_()
                except Exception:
                    pass
        await context.route(re.compile(r"noti\.worksmobile\.com|wmqtt", re.I), _block_noti)

        # ── 既読防止: WebSocket経由の既読通知をブロック（init scriptで事前注入） ──
        # LINE Worksはリアルタイム通信にWebSocketを使うため、HTTP経由のブロックだけでは不十分。
        # 全ページのWebSocket.send() をオーバーライドして、既読系フレームを握りつぶす。
        # page.goto() より前に add_init_script する必要がある（新規ページにのみ適用される）。
        await context.add_init_script("""
            (() => {
                if (window.__lwReadBlockInstalled) return;
                window.__lwReadBlockInstalled = true;
                window.__lwBlockedWsReads = 0;
                window.__lwBlockedWsFrames = [];

                const OriginalWebSocket = window.WebSocket;
                // MQTT パケットタイプ (上位4bit)
                //   1=CONNECT 2=CONNACK 3=PUBLISH 4=PUBACK 8=SUBSCRIBE
                //   9=SUBACK A=UNSUBSCRIBE B=UNSUBACK C=PINGREQ D=PINGRESP E=DISCONNECT
                // 読み取り専用スクレイプでは PUBLISH(3) のみ一律ブロック
                // （既読通知・タイピング中・開封等のクライアント発アクションは全てPUBLISH経由）。
                // PUBACK等は MQTT 通信維持に必要なので通す。
                const BLOCKED_MQTT_TYPES = new Set([3]);

                // LINE Worksの既読/閲覧系メッセージのパターン（文字列フレーム用）
                const READ_PATTERNS = [
                    /["']?(cmd|type|event|action|method)["']?\\s*:\\s*["']?(NOTI_)?(read|markread|updateread|readmessage|readmsg|lastread|seen|viewed|ack)/i,
                    /lastRead(MsgNo|MessageId|Id|No|Time|At|Seq)/i,
                    /(markAsRead|readStatus|readReceipt|readState|messageRead|msgRead|chatRead)/i,
                    /["']cmd["']\\s*:\\s*["']\\d*(read|ack|seen)\\d*["']/i,
                    /["']?op["']?\\s*:\\s*["']?(read|seen|ack)/i,
                ];

                function isReadStringFrame(data) {
                    try {
                        if (typeof data !== 'string') return false;
                        return READ_PATTERNS.some(p => p.test(data));
                    } catch (e) { return false; }
                }

                // バイナリデータの先頭バイトから MQTT パケットタイプを取得
                function getMqttPacketType(data) {
                    try {
                        let firstByte = null;
                        if (data instanceof ArrayBuffer) {
                            if (data.byteLength > 0) firstByte = new Uint8Array(data)[0];
                        } else if (ArrayBuffer.isView(data)) {
                            if (data.byteLength > 0) firstByte = new Uint8Array(data.buffer, data.byteOffset, data.byteLength)[0];
                        }
                        if (firstByte === null) return -1;
                        return (firstByte >> 4) & 0x0F;
                    } catch (e) { return -1; }
                }

                function extractMqttTopic(data) {
                    try {
                        let u8;
                        if (data instanceof ArrayBuffer) u8 = new Uint8Array(data);
                        else if (ArrayBuffer.isView(data)) u8 = new Uint8Array(data.buffer, data.byteOffset, data.byteLength);
                        else return '';
                        // Byte 0: packet type. Byte 1..: remaining length (VBI).
                        let pos = 1;
                        let mult = 1, remLen = 0;
                        for (let k = 0; k < 4; k++) {
                            if (pos >= u8.length) return '';
                            const b = u8[pos++];
                            remLen += (b & 127) * mult;
                            mult *= 128;
                            if ((b & 128) === 0) break;
                        }
                        // Topic length (2 bytes, big endian)
                        if (pos + 2 > u8.length) return '';
                        const topicLen = (u8[pos] << 8) | u8[pos + 1];
                        pos += 2;
                        if (topicLen <= 0 || topicLen > 200 || pos + topicLen > u8.length) return '';
                        return new TextDecoder('utf-8', {fatal: false}).decode(u8.slice(pos, pos + topicLen));
                    } catch (e) { return ''; }
                }

                function logBlocked(label, preview) {
                    try {
                        window.__lwBlockedWsReads++;
                        window.__lwBlockedWsFrames.push(`${label} ${preview}`.substring(0, 250));
                        if (window.__lwBlockedWsFrames.length > 50) window.__lwBlockedWsFrames.shift();
                        console.log('[LW-BLOCK]', label, preview);
                    } catch (e) {}
                }

                function patchSend(ws, isMqtt) {
                    const origSend = ws.send.bind(ws);
                    ws.send = function(data) {
                        try {
                            // MQTT接続: PUBLISH系を全ブロック（既読通知・タイピング・開封等を含む）
                            if (isMqtt) {
                                const pt = getMqttPacketType(data);
                                if (BLOCKED_MQTT_TYPES.has(pt)) {
                                    const topic = extractMqttTopic(data);
                                    const label = `MQTT-type${pt}`;
                                    const preview = `len=${data && (data.byteLength || data.size || data.length) || '?'} topic=${topic}`;
                                    logBlocked(label, preview);
                                    return;  // 送信しない
                                }
                                // CONNECT / SUBSCRIBE / PINGREQ 等は通す
                            }

                            // 文字列フレーム: 既読パターンにマッチすればブロック
                            if (isReadStringFrame(data)) {
                                logBlocked('STR-READ', String(data).substring(0, 200));
                                return;
                            }
                        } catch (e) {}
                        return origSend(data);
                    };
                }

                function PatchedWS(url, protocols) {
                    const urlStr = String(url || '');
                    const isMqtt = /wmqtt|mqtt|noti\\.worksmobile/i.test(urlStr);
                    const ws = protocols !== undefined
                        ? new OriginalWebSocket(url, protocols)
                        : new OriginalWebSocket(url);
                    patchSend(ws, isMqtt);
                    if (isMqtt) {
                        console.log('[LW-WS-PATCH] MQTT WS hooked:', urlStr);
                    }
                    return ws;
                }
                PatchedWS.prototype = OriginalWebSocket.prototype;
                PatchedWS.CONNECTING = OriginalWebSocket.CONNECTING;
                PatchedWS.OPEN = OriginalWebSocket.OPEN;
                PatchedWS.CLOSING = OriginalWebSocket.CLOSING;
                PatchedWS.CLOSED = OriginalWebSocket.CLOSED;
                window.WebSocket = PatchedWS;

                // fetch/XHR の beacon 型既読もブロック（sendBeacon 経由）
                if (navigator.sendBeacon) {
                    const origBeacon = navigator.sendBeacon.bind(navigator);
                    navigator.sendBeacon = function(url, data) {
                        const u = (url || '').toLowerCase();
                        const READ_URL_KW = ['read', 'mark', 'seen', 'ack', 'receipt', 'viewed', 'opened', 'lastread', 'noti'];
                        if (READ_URL_KW.some(kw => u.includes(kw)) ||
                            (data && typeof data === 'string' && isReadStringFrame(data))) {
                            logBlocked('BEACON', u.substring(0, 120));
                            return true;  // 送信したと偽装
                        }
                        return origBeacon(url, data);
                    };
                }
            })();
        """)

        page = await context.new_page()

        # 注: 既読通知の MQTT (wss://*-noti.worksmobile.com/wmqtt) は SharedWorker 内で動くため、
        # page-scope の遮断 (CDP Network.setBlockedURLs / context.route / WebSocket patch) は
        # 届かないことを実測で確認済。根治は browser 起動フラグ --host-resolver-rules による
        # noti ホストの DNS 遮断 (上の chromium.launch を参照)。context.route は HTTP 経路の
        # 念のための二重化として残す。

        # ── HTTP経由の既読リクエストをブロック（goto より前に登録） ──
        # URL/Method/POST bodyの3軸でチェックする。
        # GET/DELETE も含め、全メソッドを対象にする（保守的に）。
        _blocked_count = {"http": 0}
        _blocked_samples = []  # 診断用サンプル

        READ_URL_KEYWORDS = (
            "read", "mark", "seen", "receipt", "ack",
            "viewed", "opened", "visit",
            "lastread", "readstatus", "readmessage", "readstate",
            "noti/read", "noti/status", "chatstatus",
            "msgstatus", "msg-status",
        )
        READ_BODY_PATTERNS = (
            '"read"', '"markasread"', '"lastreadid"', '"lastreadmsgno"',
            '"lastreadseq"', '"readmessage"', '"readreceipt"', '"readstatus"',
            '"read_status"', '"messageread"', '"msgread"', '"chatread"',
            '"type":"read"', '"type":"noti_read"', '"cmd":"read"',
            '"action":"read"', '"event":"read"',
        )

        async def block_read_receipts(route):
            try:
                url_lower = route.request.url.lower()
                method = route.request.method

                # URL判定
                if any(kw in url_lower for kw in READ_URL_KEYWORDS):
                    _blocked_count["http"] += 1
                    if len(_blocked_samples) < 20:
                        _blocked_samples.append(f"{method} {route.request.url[:120]}")
                    logger.debug(f"既読HTTPブロック(url): {method} {route.request.url[:100]}")
                    await route.abort()
                    return

                # POST/PUT/PATCH body判定
                if method in ("POST", "PUT", "PATCH"):
                    try:
                        body = (route.request.post_data or "").lower()
                    except Exception:
                        body = ""
                    if body and any(pat in body for pat in READ_BODY_PATTERNS):
                        _blocked_count["http"] += 1
                        if len(_blocked_samples) < 20:
                            _blocked_samples.append(f"{method} {route.request.url[:80]} body={body[:80]}")
                        logger.debug(f"既読HTTPブロック(body): {method} {route.request.url[:100]}")
                        await route.abort()
                        return

                await route.continue_()
            except Exception as e:
                # route handler 内で例外が出るとページが固まるので防御
                try:
                    await route.continue_()
                except Exception:
                    pass
                logger.debug(f"block_read_receipts error (ignored): {e}")

        await page.route("**/*", block_read_receipts)

        # WebSocketイベント記録（検証用、ブロックはできないがURL把握に使う）
        # WebSocket 検知 + wmqtt 接続失敗 (= DNS 遮断成功) の追跡
        _ws_urls = []
        _wmqtt_stat = {"seen": 0, "failed": 0}

        def _on_ws(ws):
            _ws_urls.append(ws.url)
            u = ws.url.lower()
            if "wmqtt" in u or "noti.worksmobile" in u:
                _wmqtt_stat["seen"] += 1
                # DNS 遮断が効いていれば socketerror / close が即発火する
                ws.on("socketerror", lambda *_a: _wmqtt_stat.__setitem__("failed", _wmqtt_stat["failed"] + 1))
                ws.on("close", lambda *_a: _wmqtt_stat.__setitem__("failed", _wmqtt_stat["failed"] + 1))

        page.on("websocket", _on_ws)

        logger.info("既読防止モード: ON (HTTP + WebSocket + sendBeacon)")

        await page.goto(LINEWORKS_URL, wait_until="domcontentloaded", timeout=60000)

        # ログインが必要か確認
        if "login" in page.url.lower() or "auth" in page.url.lower():
            if login_id and login_pw:
                logger.info(f"自動ログイン: {login_id}")
                # ★2026-06-08 fix: LINE Works login の ID 欄は「@owndays」ドメインが
                # 右側に固定表示され、**@ の前 (= username 部) のみ**を入力する仕様。
                # 旧実装は full の「your-account@your-lw-domain」を入れていたため
                # 「@の前の部分のIDのみ入力してください」エラーで login 失敗していた。
                # @ があれば前半のみ、無ければそのまま使う。
                username_only = login_id.split("@")[0] if "@" in login_id else login_id
                # ID入力
                await page.fill('input[type="text"], input[placeholder*="id"], input[placeholder*="電話"]', username_only)
                await page.click('button:has-text("ログイン")')
                await asyncio.sleep(2)
                # パスワード入力
                pw_input = page.locator('input[type="password"]')
                if await pw_input.count() > 0:
                    await pw_input.fill(login_pw)
                    await page.click('button:has-text("ログイン")')
                # トークページに遷移するまで待つ
                await page.wait_for_url("**/talk.worksmobile.com/**", timeout=30000)
            elif not headless:
                logger.info("ブラウザでログインしてください...")
                await page.wait_for_url("**/talk.worksmobile.com/**", timeout=120000)
            else:
                await browser.close()
                logger.error("ログインにはcredentialsまたは--no-headlessが必要です。")
                return
            # Cookie保存
            cookies = await context.cookies()
            COOKIE_FILE.parent.mkdir(parents=True, exist_ok=True)
            COOKIE_FILE.write_text(json.dumps(cookies))
            logger.info("Cookieを保存しました。次回以降は自動ログインします。")

        # （既読ブロッカーは goto 前に登録済み）

        await page.wait_for_selector(".chat_grp_lst", timeout=15000)
        logger.info("トークルームリストを取得中...")

        # ── ルームリストをスクロール＆累積して全ルーム名を収集（virtualization対策） ──
        # 仮想化リストで DOM の li が ~20 件ずつローテートされるため、
        # スクロール中に見えたルーム名を Set に累積していく。
        if backfill or max_rooms > 20:
            room_names_all = await page.evaluate(
                """async ({waitMs, stableRounds, maxIters, stepPct}) => {
                    const sleep = (ms) => new Promise(r => setTimeout(r, ms));

                    // スクロール可能コンテナを特定
                    const list = document.querySelector('.chat_grp_lst') ||
                                 document.querySelector('[class*="room_list"]') ||
                                 document.querySelector('[class*="chat_list"]');
                    let scroller = list;
                    while (scroller && scroller !== document.body) {
                        if (scroller.scrollHeight > scroller.clientHeight + 50) break;
                        scroller = scroller.parentElement;
                    }
                    if (!scroller) scroller = list || document.body;

                    // 一番上に戻してから開始
                    scroller.scrollTop = 0;
                    await sleep(waitMs);

                    const names = new Set();
                    const collect = () => {
                        document.querySelectorAll('li.item_chat').forEach(li => {
                            const n = li.querySelector('strong')?.textContent?.trim();
                            if (n) names.add(n);
                        });
                    };
                    collect();

                    let stable = 0;
                    let iters = 0;
                    for (let i = 0; i < maxIters; i++) {
                        iters++;
                        const before = names.size;
                        // 下にスクロール（80% 刻み）
                        scroller.scrollTop += Math.max(200, scroller.clientHeight * stepPct);
                        await sleep(waitMs);
                        collect();
                        if (names.size === before) {
                            stable++;
                            if (stable >= stableRounds) break;
                        } else {
                            stable = 0;
                        }
                    }
                    // 最後まで行ったら念のため最下部に到達を確認
                    scroller.scrollTop = scroller.scrollHeight;
                    await sleep(waitMs);
                    collect();

                    // 元に戻す
                    scroller.scrollTop = 0;
                    await sleep(300);
                    return {names: Array.from(names), iters, finalCount: names.size};
                }""",
                {"waitMs": 700, "stableRounds": 4, "maxIters": 80, "stepPct": 0.8},
            )
            room_name_list = room_names_all["names"]
            logger.info(
                f"ルームリストスクロール: {room_names_all['finalCount']} 件 "
                f"({room_names_all['iters']} iters)"
            )
            rooms = [{"name": n, "preview": ""} for n in room_name_list]
        else:
            # 差分モード（top 20 だけ）
            rooms = await page.evaluate("""() => {
                const items = document.querySelectorAll('li.item_chat');
                return Array.from(items).map(li => ({
                    name: li.querySelector('strong')?.textContent?.trim() || '',
                    preview: li.querySelector('dd.msg')?.textContent?.trim()?.substring(0, 50) || '',
                }));
            }""")
        logger.info(f"トークルームリスト: {len(rooms)} 件 検出")

        # 常に --rooms で上限を適用（バックフィルでも指定可能）
        rooms = rooms[:max_rooms]
        if backfill:
            logger.info(f"バックフィルモード: {len(rooms)} ルームをスクロールバック取得")
        else:
            logger.info(f"差分モード: {len(rooms)} トークルーム（最新N件）を処理します")

        # 既取込ハッシュをロード（差分取得用）
        state = _load_state()
        all_exports = []

        for i, room in enumerate(rooms):
            room_name = room["name"]
            logger.info(f"[{i+1}/{len(rooms)}] {room_name}")

            # ルーム名でクリック（仮想化リストに対応: 必要ならリストをスクロールして探索）
            clicked = await page.evaluate(
                """async ({targetName, maxScrollIters, waitMs}) => {
                    const sleep = (ms) => new Promise(r => setTimeout(r, ms));

                    const findAndClick = () => {
                        const items = document.querySelectorAll('li.item_chat');
                        for (const li of items) {
                            const name = li.querySelector('strong')?.textContent?.trim();
                            if (name === targetName) {
                                li.click();
                                return true;
                            }
                        }
                        return false;
                    };

                    // まず現在見えてる範囲でクリック
                    if (findAndClick()) return true;

                    // 見えてなければリストをスクロールしながら探す
                    const list = document.querySelector('.chat_grp_lst') ||
                                 document.querySelector('[class*="room_list"]');
                    let scroller = list;
                    while (scroller && scroller !== document.body) {
                        if (scroller.scrollHeight > scroller.clientHeight + 50) break;
                        scroller = scroller.parentElement;
                    }
                    if (!scroller) return false;

                    // 先頭からスクロールしながら探索
                    scroller.scrollTop = 0;
                    await sleep(waitMs);
                    if (findAndClick()) return true;

                    for (let i = 0; i < maxScrollIters; i++) {
                        scroller.scrollTop += Math.max(200, scroller.clientHeight * 0.8);
                        await sleep(waitMs);
                        if (findAndClick()) return true;
                        // ボトム到達チェック
                        if (scroller.scrollTop + scroller.clientHeight >= scroller.scrollHeight - 5) {
                            break;
                        }
                    }
                    return false;
                }""",
                {"targetName": room_name, "maxScrollIters": 80, "waitMs": 600},
            )
            if not clicked:
                logger.warning(f"  ルームをクリックできず（スキップ）: {room_name}")
                continue

            await asyncio.sleep(1.8)  # メッセージ読み込み待ち

            # ─── スクロールバックで履歴をロード ───
            if backfill:
                scroll_result = await _scroll_to_top(page)
                logger.info(
                    f"  スクロール結果: {scroll_result.get('count', 0)} msgs loaded "
                    f"in {scroll_result.get('iters', 0)} iters "
                    f"({scroll_result.get('reason', 'ok')})"
                )
            else:
                # 差分モードでも軽めにスクロール（数百件まで遡って新規を拾う）
                scroll_result = await _scroll_to_top(page, max_iters=15)

            # メッセージ・ファイル・リンクを抽出（日付セパレータも順序保持で解析）
            room_data = await page.evaluate("""() => {
                // メッセージエリアのコンテナを特定
                let container = document.querySelector('.chat_view') ||
                                document.querySelector('.msg_area') ||
                                document.body;

                // 日付パターン検出: 「2026年4月15日」「4月15日」「4/15」「2026/4/15」「Apr 15」等
                const datePat = /(\\d{4}[年/\\-]\\d{1,2}[月/\\-]\\d{1,2}日?|\\d{1,2}[月/\\-]\\d{1,2}日?(\\s|$)|[A-Za-z]{3,9}\\s+\\d{1,2}(,?\\s+\\d{4})?)/;

                // 全子孫を DOM 順に走査（msg_wrap と、それ以外で日付らしきテキストのもの）
                const all = container.querySelectorAll('*');
                const msgs = [];
                let currentDate = '';
                const visitedMsgWraps = new Set();

                for (const node of all) {
                    // .msg_wrap の処理
                    if (node.classList && node.classList.contains('msg_wrap')) {
                        if (visitedMsgWraps.has(node)) continue;
                        visitedMsgWraps.add(node);

                        const w = node;
                        const isMyMsg = w.classList.contains('msg_rgt');
                        const dateEl = w.querySelector('[class*="date"]');
                        const nameEl = w.querySelector('.name');
                        const sender = isMyMsg ? 'me' : (nameEl?.textContent?.trim() || 'other');
                        const time = dateEl?.textContent?.trim() || '';

                        // message の title/tooltip から full timestamp 取得を試みる
                        let fullTs = '';
                        const tsHost = w.querySelector('[title*="2025"], [title*="2026"], [title*="2024"], [title*="年"], [title*="/"]');
                        if (tsHost) fullTs = tsHost.getAttribute('title') || '';

                        // テキストメッセージ
                        const msgEl = w.querySelector('.msg_box p.msg');
                        if (msgEl) {
                            msgs.push({
                                type: 'text',
                                text: msgEl.textContent?.trim() || '',
                                sender, time, date: currentDate, fullTs
                            });
                        }

                        // ファイル添付
                        const attach = w.querySelector('div.attach');
                        if (attach) {
                            const fname = attach.querySelector('.file_name')?.textContent?.trim() || '';
                            const finfo = attach.querySelector('.file_info')?.textContent?.trim() || '';
                            msgs.push({
                                type: 'file',
                                text: `[ファイル] ${fname} (${finfo})`,
                                sender, time, date: currentDate, fullTs, fileName: fname
                            });
                        }

                        // 画像メッセージ
                        const imgMsg = w.querySelector('.msg_img img, .img_area img');
                        if (imgMsg) {
                            const src = imgMsg.src || '';
                            msgs.push({
                                type: 'image',
                                text: '[画像]',
                                sender, time, date: currentDate, fullTs, imageUrl: src
                            });
                        }
                        continue;
                    }

                    // msg_wrap 以外: 短いテキストで日付パターンにマッチすれば日付セパレータ
                    if (!node.children || node.children.length === 0) continue;
                    if (node.childElementCount > 3) continue;  // 複雑なノードは無視
                    const txt = (node.textContent || '').trim();
                    if (!txt || txt.length > 40) continue;
                    if (datePat.test(txt)) {
                        currentDate = txt;
                    }
                }

                // チャット内のリンクも抽出
                const chatView = document.querySelector('.chat_view');
                const links = [];
                if (chatView) {
                    const anchors = chatView.querySelectorAll('a[href*="http"]');
                    for (const a of anchors) {
                        const href = a.href || '';
                        if (href.includes('docs.google.com') || href.includes('drive.google.com') ||
                            href.includes('sheets.google.com') || href.includes('.pdf') ||
                            href.includes('.xlsx') || href.includes('.docx')) {
                            links.push(href);
                        }
                    }
                }

                return { messages: msgs, links: [...new Set(links)] };
            }""")

            messages_raw = room_data.get("messages", [])
            shared_links = room_data.get("links", [])

            # ─── 重複排除: 既取込ハッシュと照合 ───
            seen_hashes = set(state.get(room_name, []))
            new_messages = []
            new_hashes_this_run = []

            for m in messages_raw:
                h = _msg_hash(m)
                if h in seen_hashes:
                    continue
                seen_hashes.add(h)
                new_hashes_this_run.append(h)
                new_messages.append(m)

            # 未取込の新規分だけを以降で処理
            messages = new_messages
            if messages_raw and not messages:
                logger.info(f"  全 {len(messages_raw)} msg は取込済み（新規なし）")

            # ─── コンテンツ深堀り ───
            extracted_contents = []
            http_client = httpx.AsyncClient(timeout=30.0)

            # Google Docs/Sheets のテキスト取得
            for link in shared_links:
                text = await extract_google_doc_via_playwright(link, page)
                if text:
                    extracted_contents.append(f"[Google Doc: {link[:60]}]\n{text}")

            # 添付ファイルのダウンロード → テキスト抽出
            attach_idx = 0
            for m in messages:
                if m.get("type") == "file":
                    file_path = await download_file_from_chat(page, attach_idx)
                    if file_path:
                        text = await extract_file_text(file_path)
                        if text:
                            extracted_contents.append(
                                f"[ファイル: {m.get('fileName', file_path.name)}]\n{text}"
                            )
                        file_path.unlink(missing_ok=True)
                    attach_idx += 1

                # 画像のVision API解析
                if m.get("type") == "image" and m.get("imageUrl"):
                    text = await extract_image_text(
                        m["imageUrl"], http_client, LITELLM_URL, LITELLM_KEY
                    )
                    if text:
                        m["text"] = f"[画像] {text}"

            await http_client.aclose()

            export = None
            if messages or shared_links or extracted_contents:
                export = {
                    "room": room_name,
                    "messages": messages,
                    "links": shared_links,
                    "extracted": extracted_contents,
                    "scraped_at": datetime.now().isoformat(),
                    "new_hashes": new_hashes_this_run,
                    "total_seen": len(messages_raw),
                }
                all_exports.append(export)
                file_count = sum(1 for m in messages if m.get("type") in ("file", "image"))
                ext_count = len(extracted_contents)
                logger.info(
                    f"  → {len(messages)} NEW msg / {len(messages_raw)} visible, "
                    f"{file_count} file/img, {len(shared_links)} link, {ext_count} extracted"
                )

            # state は成功時のみ更新
            if new_hashes_this_run:
                prev = state.get(room_name, [])
                # 最新20000件まで保持（ルームあたり~1MB上限）
                state[room_name] = (prev + new_hashes_this_run)[-20000:]

            # ─── クラッシュ耐性: このルーム分を即時保存 ───
            if not dry_run and export:
                try:
                    _write_export_file(export, backfill=backfill)
                except Exception as e:
                    logger.error(f"  保存失敗: {e}")

            # state ファイルも毎回更新（1ルームごと、書込みは軽量）
            if not dry_run:
                try:
                    _save_state(state)
                except Exception as e:
                    logger.error(f"  state保存失敗: {e}")

        # ── 既読ブロック診断サマリ（ブラウザ閉じる前に取得） ──
        try:
            ws_blocked = await page.evaluate(
                "() => ({count: window.__lwBlockedWsReads || 0, "
                "samples: (window.__lwBlockedWsFrames || []).slice(-5)})"
            )
        except Exception:
            ws_blocked = {"count": 0, "samples": []}

        logger.info(
            f"既読ブロック統計: HTTP={_blocked_count['http']}件, "
            f"WebSocket(patch)={ws_blocked.get('count', 0)}件, "
            f"noti/wmqtt HTTP遮断={_noti_blocked['n']}件 (本命は DNS 遮断、下記 wmqtt 接続を参照)"
        )
        if _blocked_samples:
            logger.info(f"  HTTPブロックサンプル (up to 5): ")
            for s in _blocked_samples[:5]:
                logger.info(f"    {s[:150]}")
        if ws_blocked.get("samples"):
            logger.info(f"  WSブロックサンプル (up to 5):")
            for s in ws_blocked["samples"][:5]:
                logger.info(f"    {s[:150]}")
        if _ws_urls:
            unique_ws = list(set(_ws_urls))[:3]
            logger.info(f"  検知したWebSocket URL: {len(set(_ws_urls))} 件 (例: {unique_ws})")

        # ★2026-06-08: wmqtt (既読通知の主経路) を遮断できたかが本質。
        # DNS 遮断 (--host-resolver-rules) が効けば wmqtt は接続失敗 (seen>0 かつ failed>0)。
        logger.info(
            f"  wmqtt 接続: 検知 {_wmqtt_stat['seen']} / 失敗(遮断) {_wmqtt_stat['failed']}"
        )
        if _wmqtt_stat["seen"] > 0 and _wmqtt_stat["failed"] >= _wmqtt_stat["seen"]:
            logger.info("  ✓✓ 既読通知ソケット (wmqtt) を DNS 遮断 = 既読は付かない (根治成功)")
        elif _wmqtt_stat["seen"] > 0 and _wmqtt_stat["failed"] == 0:
            logger.warning(
                "⚠️  wmqtt が接続成立した可能性 (failed=0) = 既読が素通りした恐れ。"
                "--host-resolver-rules のパターン要確認 (= noti ホスト名が想定外?)。"
            )
        elif _wmqtt_stat["seen"] == 0:
            logger.info("  wmqtt 未検知 (= 既読通知ソケットが発生しなかった / 完全遮断)。")

        await browser.close()

        if dry_run:
            for export in all_exports:
                print(f"\n{'='*50}")
                ext = export.get('extracted', [])
                print(f"Room: {export['room']} ({len(export['messages'])} NEW / {export.get('total_seen', 0)} visible, {len(export.get('links',[]))} links, {len(ext)} extracted)")
                for m in export["messages"][:3]:
                    sender = "You" if m["sender"] == "me" else m["sender"]
                    mtype = f"[{m.get('type','text')}]" if m.get("type") != "text" else ""
                    d = m.get("date", "")
                    print(f"  [{d} {m.get('time','')}] [{sender}] {mtype}{m['text'][:80]}")
                print(f"  ... ({len(export['messages'])-6} more) ..." if len(export['messages']) > 6 else "")
                for m in export["messages"][-3:]:
                    sender = "You" if m["sender"] == "me" else m["sender"]
                    mtype = f"[{m.get('type','text')}]" if m.get("type") != "text" else ""
                    d = m.get("date", "")
                    print(f"  [{d} {m.get('time','')}] [{sender}] {mtype}{m['text'][:80]}")
                for link in export.get("links", []):
                    print(f"  [LINK] {link[:80]}")
                for e in ext[:3]:
                    print(f"  [EXTRACTED] {e[:120]}...")
            return all_exports

        logger.info(f"完了: {len(all_exports)} ルーム → data/brain/import/ (ファイルウォッチャーが自動取り込みします)")
        logger.info(f"state 更新: {STATE_FILE.name} ({sum(len(v) for v in state.values())} total hashes tracked)")
        return all_exports


def _write_export_file(export: dict, backfill: bool = False) -> None:
    """単一ルームのエクスポートをファイルに書き出す（ファイルウォッチャーが自動取込）。"""
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    today = date.today().isoformat()
    room_slug = export["room"].replace("/", "_").replace(" ", "_")[:30]
    if backfill:
        filename = f"lineworks_{room_slug}_backfill_{today}.txt"
    else:
        filename = f"lineworks_{room_slug}_{today}.txt"
    filepath = OUTPUT_DIR / filename

    lines = []
    current_date_header = None
    for m in export["messages"]:
        sender = "海山丈司" if m["sender"] == "me" else m["sender"]
        time_str = m.get("time", "")
        msg_date = m.get("date", "")
        if msg_date and msg_date != current_date_header:
            lines.append(f"--- {msg_date} ---")
            current_date_header = msg_date
        lines.append(f"{time_str}\t{sender}\t{m['text']}")

    if export.get("links"):
        lines.append("")
        lines.append("[共有リンク]")
        for link in export["links"]:
            lines.append(f"\t\t{link}")

    if export.get("extracted"):
        lines.append("")
        lines.append("[抽出コンテンツ]")
        for content in export["extracted"]:
            lines.append(content)

    header = f"[LINE Works] {export['room']}\n{today}"
    if backfill:
        header += f" (backfill: {len(export['messages'])} msgs)"
    content = header + "\n\n" + "\n".join(lines)
    filepath.write_text(content, encoding="utf-8")
    logger.info(f"  保存: {filepath.name} ({len(export['messages'])} msg)")


def _acquire_lock():
    """排他ロック取得（同時実行防止）。取れなければ None を返す。"""
    LOCK_FILE.parent.mkdir(parents=True, exist_ok=True)
    fp = open(LOCK_FILE, "w")
    try:
        fcntl.flock(fp.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        fp.close()
        return None
    # PID を書き込んで他プロセスから確認可能に
    fp.write(f"{os.getpid()} {datetime.now().isoformat()}\n")
    fp.flush()
    return fp


async def main():
    parser = argparse.ArgumentParser(description="LINE Works トーク スクレイパー")
    parser.add_argument("--rooms", type=int, default=20, help="処理するトークルーム数（--backfill時は無視=全ルーム）")
    parser.add_argument("--dry-run", action="store_true", help="取り込みせずにプレビュー")
    parser.add_argument("--no-headless", action="store_true", help="ブラウザを表示する")
    # ★2026-06-08 システム評価 0-1: 認証情報は env 経由 (§1.1 secret は os.getenv() のみ)。
    # 変数名は .env.example の既存規約 LINEWORKS_USER / LINEWORKS_PASS に合わせる。
    # CLI で渡せば override 可。scrape_cron.sh からは env で供給 (平文 hardcode を除去)。
    parser.add_argument("--login-id", default=os.getenv("LINEWORKS_USER", ""),
                        help="LINE Works ログインID (未指定時 env LINEWORKS_USER)")
    parser.add_argument("--login-pw", default=os.getenv("LINEWORKS_PASS", ""),
                        help="LINE Works パスワード (未指定時 env LINEWORKS_PASS)")
    parser.add_argument("--backfill", action="store_true",
                        help="全ルーム・全履歴をスクロールバックで取得（初回のみ推奨、時間かかります）")
    parser.add_argument("--force", action="store_true",
                        help="他プロセスが動作中でも強制実行（ロック無視）")
    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s")

    # ── 排他ロック（cron と手動実行の衝突を防ぐ） ──
    lock_fp = None
    if not args.force:
        lock_fp = _acquire_lock()
        if lock_fp is None:
            existing = ""
            try:
                existing = LOCK_FILE.read_text().strip()
            except Exception:
                pass
            logger.warning(
                f"別のインスタンスが実行中のためスキップします ({LOCK_FILE.name}: {existing}). "
                f"強制実行する場合は --force を指定してください。"
            )
            sys.exit(0)

    try:
        await scrape_lineworks(
            max_rooms=args.rooms,
            dry_run=args.dry_run,
            headless=not args.no_headless,
            login_id=args.login_id,
            login_pw=args.login_pw,
            backfill=args.backfill,
        )
    finally:
        if lock_fp is not None:
            try:
                fcntl.flock(lock_fp.fileno(), fcntl.LOCK_UN)
                lock_fp.close()
                LOCK_FILE.unlink(missing_ok=True)
            except Exception as e:
                logger.warning(f"lock release failed: {e}")


if __name__ == "__main__":
    asyncio.run(main())
