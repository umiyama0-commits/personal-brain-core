#!/usr/bin/env python3
"""scripts/claude_personal_sync.py — Claude.ai の Example Garden 会話だけを personal ドメインへ取り込む。

★2026-06-28 海山指示「Example の会話だけを personal へ」。claude_scraper.py の実証済 Playwright DOM を
踏襲しつつ、本スクリプトは:
  1. サイドバーの会話 **title が Example marker に合致するものだけ** を対象 (非合致は開かず保存しない=privacy)。
  2. 各 Example 会話を LLM で **abstract (要約)** — raw 会話を dump せず、会話に実在する事実/決定/論点だけ
     を構造化 (捏造禁止)。
  3. data/brain/wiki/personal/example-garden/conversations/<id>.md に **private** で書く
     (= OWNDAYS file-watcher を経由させない。§1.17 で OWNDAYS 全経路から除外)。
  4. 取り込み後に personal_snapshot で版管理。

不変条件: 出力先は personal のみ (OWNDAYS には一切流さない)。Example 非合致会話の本文は保存しない。
admin 運用 (Mac Studio、要ログイン済 profile)。state で処理済 id を保持し再要約しない。

実行: python3 scripts/claude_personal_sync.py [--count N|--dry-run|--no-headless]
"""
from __future__ import annotations

import argparse
import asyncio
import contextlib
import json
import logging
import os
import re
import sys
import time
from datetime import date, datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))   # scripts/ sibling import

from clone_improve_lib import call_llm, line_push, line_push_digest  # noqa: E402

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger("claude_personal_sync")

ROOT = Path(__file__).resolve().parents[1]
PROFILE_DIR = ROOT / "data" / "brain" / ".scraper-profiles" / "claude"   # claude_scraper と共有(ログイン済)
STATE_FILE = ROOT / "data" / "brain" / ".example_sync_state.json"
DEST_DIR = ROOT / "data" / "brain" / "wiki" / "personal" / "example-garden" / "conversations"
CLAUDE_URL = "https://claude.ai"

# Example 会話の title 合致 marker (表記揺れ込み)。これに合致しない会話は開かない=保存しない。
PERSONAL_RE = re.compile(r"example|example|Example Garden|example[\s_-]*garden", re.IGNORECASE)

# ★PJ ページ URL を .env に設定すると、その PJ の全会話を列挙して取り込む(title 無関係=PJ で全部、
# 海山指示 2026-06-29)。未設定なら従来どおり title 合致のみ(後方互換)。例: PERSONAL_PROJECT_URL=https://claude.ai/project/<uuid>
PERSONAL_PROJECT_URL = os.getenv("PERSONAL_PROJECT_URL", "").strip()

ABSTRACT_PROMPT = """以下は Claude.ai での「{title}」という Example Garden(海山個人の非OWNDAYS PJ)
関連の会話です。個人の PJ 記録として、**会話に実際に出てきた事実・決定・論点・次アクション**を構造的に
要約してください。

# 絶対制約
- 会話に書かれていることだけ。創作・推測で補わない。曖昧なら「(未確定)」と書く。
- 数値・固有名詞・日付は会話のまま。捏造しない。
- これは OWNDAYS とは無関係の個人 PJ 記録。率直で良い。

# 出力 (markdown 本文のみ、前置き不要)
## 要点
- ...
## 決定・方針
- ...
## 論点・未確定
- ...
## 次アクション
- ...

# 会話
{conversation}"""


def load_state() -> dict:
    if STATE_FILE.exists():
        try:
            return json.loads(STATE_FILE.read_text(encoding="utf-8"))
        except Exception:
            pass
    return {"done_ids": [], "last_run": ""}


def save_state(state: dict) -> None:
    state["last_run"] = datetime.now().isoformat(timespec="seconds")
    STATE_FILE.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")


def is_example_title(title: str) -> bool:
    return bool(PERSONAL_RE.search(title or ""))


def _conv_to_text(messages: list[dict], cap: int = 24_000) -> str:
    out, acc = [], 0
    for m in messages:
        who = "海山" if m.get("role") == "user" else "Claude"
        line = f"{who}: {(m.get('content') or '').strip()}"
        if acc + len(line) > cap:
            out.append("…(truncated)")
            break
        out.append(line)
        acc += len(line)
    return "\n\n".join(out)


async def abstract_conversation(title: str, messages: list[dict], llm=None) -> str:
    """Example 会話 → 構造化要約 (捏造禁止)。raw は保存しない。"""
    llm = llm or call_llm
    convo = _conv_to_text(messages)
    if len(convo) < 40:
        return ""
    prompt = ABSTRACT_PROMPT.format(title=title, conversation=convo)
    out = await llm(prompt, model="smart", max_tokens=1800, temperature=0.2)
    return (out or "").strip()


def _slugify(s: str) -> str:
    s = re.sub(r"[^a-z0-9]+", "-", (s or "").lower()).strip("-")
    return s[:48] or "conv"


def write_personal_abstract(conv_id: str, title: str, abstract: str) -> Path:
    """要約を personal/example-garden/conversations/ に private で書く (OWNDAYS 非経由)。
    ★scrape と export(+alignment)の複数経路が同じ会話を二重取込しないよう conv_id で既存 check(dedup)。"""
    DEST_DIR.mkdir(parents=True, exist_ok=True)
    cid = (conv_id or "")[:8]
    if cid:
        existing = next(iter(DEST_DIR.glob(f"*-{cid}.md")), None)
        if existing is not None:
            return existing                             # 別経路含め取込済 → skip(二重取込防止)
    fname = f"{date.today().isoformat()}-{_slugify(title)}-{cid}.md"
    path = DEST_DIR / fname
    body = (
        f"---\nclone_visibility: private\ndomain: personal\nproject: example-garden\n"
        f"source: claude.ai\nsource_conv_id: {conv_id}\nimported: {date.today().isoformat()}\n---\n"
        f"# Example 会話要約: {title}\n\n{abstract}\n"
    )
    path.write_text(body, encoding="utf-8")
    return path


# ── 以下 Playwright スクレイプ (claude_scraper.py の実証済 DOM を踏襲) ──

_LIST_JS = """() => {
    const items = [];
    const links = document.querySelectorAll('a[href*="/chat/"]');
    for (const a of links) {
        const href = a.getAttribute('href') || '';
        const m = href.match(/\\/chat\\/([a-f0-9-]+)/);
        if (m) items.push({ id: m[1], title: (a.textContent || 'Untitled').trim(), href });
    }
    return items;
}"""

_MSG_JS = """() => {
    const results = [];
    const turns = document.querySelectorAll('[data-testid="user-message"], [data-testid="assistant-message"]');
    if (turns.length > 0) {
        for (const el of turns) {
            const t = el.getAttribute('data-testid') || '';
            const role = t.includes('user') ? 'user' : 'assistant';
            const text = (el.innerText || '').trim();
            if (text) results.push({ role, content: text });
        }
        return results;
    }
    const userEls = document.querySelectorAll('.font-user-message');
    const aEls = document.querySelectorAll('.font-claude-message');
    const all = [...Array.from(userEls).map(el => ({ el, role: 'user' })),
                 ...Array.from(aEls).map(el => ({ el, role: 'assistant' }))]
        .sort((a, b) => (a.el.compareDocumentPosition(b.el) & Node.DOCUMENT_POSITION_FOLLOWING) ? -1 : 1);
    for (const { el, role } of all) {
        const text = (el.innerText || '').trim();
        if (text) results.push({ role, content: text });
    }
    return results;
}"""


def _alert_login_needed(state: dict) -> None:
    """claude.ai ログイン不能を LINE 通知。
    ★codex 教訓: scrape が静かに止まらないよう、失敗を loud に(2週間気付かなかった事故の再発防止)。

    ★2026-08-03 実測で判明した「鳴っているのに効かない」型の失敗:
    2026-06-28 の初回実行から 51 回連続 login_required = **一度も成功していない**
    (= セッション切れではなく初回ログインが未実施)のに、文面は「切れています」で固定、
    3日 cooldown で 50 日間ほぼ同じ文が ~17 回ダイジェストに流れて埋もれた (§1.18 の alert 疲れ)。
    対策: ①一度も成功していない事を文面で言い分ける ②連続失敗日数を出して深刻さを可視化
    ③cooldown を段階的に伸ばす (3→7→14日) = 鳴り止まないが繰り返しは減る。
    """
    fails = int(state.get("login_fail_streak", 0))
    alerts = int(state.get("login_alert_count", 0))
    cooldown_d = 3 if alerts < 5 else (7 if alerts < 10 else 14)
    if time.time() - state.get("last_login_alert", 0) < cooldown_d * 86400:
        return
    # ever_succeeded は本 fix 以降にしか立たないので、過去の成功痕跡 (done_ids) も併せて見る
    # (§1.15 Reviewer L3: 移行期に「一度も成功していません」と誤報しないため)
    never = not state.get("ever_succeeded") and not state.get("done_ids")
    if never:
        head = ("⚠️ Example 自動取込: **一度も成功していません** "
                f"(初回ログインが未実施のまま {fails} 回連続で停止中)")
    else:
        head = f"⚠️ Example 自動取込: claude.ai のセッション切れで停止中 ({fails} 回連続)"
    try:
        line_push_digest(
            f"{head}\n"
            "Mac Studio に画面共有し `python3 scripts/claude_personal_sync.py --no-headless` を1回実行し"
            "ログインしてください (所要1分、以降は自動)。\n"
            "不要なら LaunchAgent `com.brain.personal-sync` を停止してください。\n"
            "(export→自動import の backstop は別途稼働しています)", "Example")
        state["last_login_alert"] = time.time()
        state["login_alert_count"] = alerts + 1
        save_state(state)
    except Exception as e:
        # ★§1.15 DA: 従来 `except: pass` で通知失敗を丸呑みしていた (§1.18 を引用しながら
        # §1.18 が禁じる無音 skip を alert 自身が持っていた)。timestamp を進めないので
        # 次回また試行され、失敗自体も loud_fail の streak に乗る。
        logger.error(f"ログイン通知の送信に失敗: {type(e).__name__}: {e}")
        with contextlib.suppress(Exception):
            from clone_improve_lib import loud_fail
            loud_fail("example-alert", False, f"通知送信失敗: {type(e).__name__}: {e}",
                      threshold=2, cooldown_h=48)


def _record_run(state: dict, ok: bool, detail: str = "") -> None:
    """§1.18 配線。login_required は _alert_login_needed が人向け文面を出すので二重通知を避け、
    **それ以外の失敗** (playwright 落ち / 例外 / scrape 破綻) だけ loud_fail に乗せる
    (この経路は従来 log 1 行のみで完全に無音だった)。streak は常に state に記録する。"""
    state["login_fail_streak"] = 0 if ok else int(state.get("login_fail_streak", 0)) + 1
    if ok:
        state["ever_succeeded"] = True
        state["login_alert_count"] = 0
    with contextlib.suppress(Exception):
        save_state(state)
    if detail == "login_required":
        # ★2026-08-03 §1.15 DA: playwright の一過性クラッシュは loud_fail (critical=True の
        # 配達保証チャネル) に乗るのに、**51 回連続・一度も成功していない最重症**の
        # login_required だけが最弱チャネル (digest info) に留まっていた = 深刻度が逆転していた。
        # 長期化した時点で critical へ昇格する (14 回 = 2 週間放置、以後 14 日おき)。
        if int(state.get("login_fail_streak", 0)) >= 14:
            with contextlib.suppress(Exception):
                from clone_improve_lib import loud_fail
                loud_fail("example-login", False,
                          f"claude.ai ログイン不能が {state['login_fail_streak']} 回連続 "
                          f"({'一度も成功していない' if not state.get('ever_succeeded') else 'セッション切れ'})。"
                          "`python3 scripts/claude_personal_sync.py --no-headless` を 1 回 (所要1分)、"
                          "不要なら LaunchAgent com.brain.personal-sync を停止。",
                          threshold=1, cooldown_h=24 * 14)
        return
    with contextlib.suppress(Exception):
        from clone_improve_lib import loud_fail
        loud_fail("example-sync", ok, detail, threshold=3, cooldown_h=72)


async def sync(*, max_conversations: int = 20, dry_run: bool = False, headless: bool = True,
               llm=None) -> dict:
    try:
        from playwright.async_api import async_playwright
    except ImportError:
        logger.error("playwright 未導入。pip install playwright && playwright install chromium")
        _record_run(load_state(), False, "playwright 未導入")
        return {"ok": False, "reason": "playwright 未導入"}

    state = load_state()
    done = set(state.get("done_ids", []))
    written: list[str] = []

    async with async_playwright() as p:
        PROFILE_DIR.mkdir(parents=True, exist_ok=True)
        args = ["--disable-blink-features=AutomationControlled"]
        if headless:
            args.append("--window-position=-2400,-2400")
        ctx = await p.chromium.launch_persistent_context(
            str(PROFILE_DIR), headless=False, channel="chrome",
            viewport={"width": 1280, "height": 900}, args=args)
        page = ctx.pages[0] if ctx.pages else await ctx.new_page()
        await page.goto(CLAUDE_URL, wait_until="domcontentloaded", timeout=60000)
        await asyncio.sleep(3)

        # ログイン判定 (URL + ログインボタン有無の二重チェック、claude_scraper 流儀)
        needs_login = "login" in page.url.lower() or "/auth" in page.url.lower()
        if not needs_login:
            needs_login = await page.evaluate("""() => {
                const els = document.querySelectorAll('button, a');
                return Array.from(els).some(b => {
                    const t = b.textContent || '';
                    return t.includes('ログイン') || t.includes('Log in') || t.includes('Sign in');
                });
            }""")
        if needs_login:
            if headless:
                await ctx.close()
                _record_run(state, False, "login_required")
                _alert_login_needed(state)              # ★静かに止めない(LINE 通知、3日 cooldown)
                logger.error("要ログイン。Mac Studio で `python3 scripts/claude_personal_sync.py --no-headless` を 1 回実行しログインしてください。")
                return {"ok": False, "reason": "login_required"}
            # --no-headless: 対話ログインを待つ (最大 3 分)
            logger.info("ブラウザで claude.ai にログインしてください(最大3分待機)...")
            await page.evaluate("""() => {
                const els = document.querySelectorAll('button, a');
                for (const b of els) {
                    const t = b.textContent || '';
                    if (t.includes('ログイン') || t.includes('Log in') || t.includes('Sign in')) { b.click(); break; }
                }
            }""")
            logged_in = False
            for _ in range(60):
                await asyncio.sleep(3)
                try:
                    u = page.url
                    if "claude.ai" in u and "login" not in u and "auth" not in u:
                        await asyncio.sleep(5)
                        logged_in = True
                        break
                except Exception:
                    pass
            if not logged_in:
                await ctx.close()
                logger.error("ログイン未完了 (タイムアウト)。再実行してください。")
                _record_run(state, False, "login_timeout")
                return {"ok": False, "reason": "login_timeout"}
            logger.info("ログイン完了。Example 会話の取り込みを続行します。")
            await asyncio.sleep(2)

        if PERSONAL_PROJECT_URL:
            # ★PJ ページを開いて列挙 = その PJ の全会話が対象(title 無関係)。海山指示「PJ で全部」。
            await page.goto(PERSONAL_PROJECT_URL, wait_until="domcontentloaded", timeout=60000)
            await asyncio.sleep(3)
            for _ in range(6):                          # 遅延ロードを軽くスクロール(recent を広めに拾う)
                await page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
                await asyncio.sleep(1.2)
            convos = await page.evaluate(_LIST_JS)
            targets = [c for c in convos if c["id"] not in done][:max_conversations]   # PJ内=全部
            logger.info(f"PJページ列挙 {len(convos)} 会話中 未処理 {len(targets)} 件 (project-mode)")
        else:
            # 後方互換: title 合致のみ(非合致は開かない=privacy)。PJ URL 未設定時の fallback。
            convos = await page.evaluate(_LIST_JS)
            targets = [c for c in convos if is_example_title(c["title"]) and c["id"] not in done][:max_conversations]
            logger.info(f"全{len(convos)}会話中 Example(title)未処理 {len(targets)} 件 (title-mode)")

        for i, c in enumerate(targets):
            await page.goto(f"{CLAUDE_URL}{c['href']}", wait_until="domcontentloaded", timeout=30000)
            await asyncio.sleep(2)
            prev = 0
            for _ in range(10):
                n = await page.evaluate("() => document.querySelectorAll('[data-testid=\"user-message\"],[data-testid=\"assistant-message\"],.font-user-message,.font-claude-message').length")
                if n == prev:
                    break
                prev = n
                await page.evaluate("window.scrollTo(0, 0)")
                await asyncio.sleep(1)
            messages = await page.evaluate(_MSG_JS)
            if not messages:
                logger.warning(f"  [{i+1}] メッセージ取得失敗: {c['title']}")
                continue
            abstract = await abstract_conversation(c["title"], messages, llm=llm)
            if not abstract:
                continue
            if dry_run:
                print(f"\n=== {c['title']} ({c['id'][:8]}) ===\n{abstract[:600]}")
            else:
                path = write_personal_abstract(c["id"], c["title"], abstract)
                logger.info(f"  [{i+1}] → {path.name}")
            done.add(c["id"]); written.append(c["id"])
        await ctx.close()

    if not dry_run and written:
        state["done_ids"] = list(done)[-500:]
        save_state(state)
        try:
            import subprocess
            subprocess.run(["python3", str(ROOT / "scripts" / "personal_snapshot.py")],
                           timeout=60, check=False)
        except Exception as e:
            logger.warning(f"snapshot 失敗: {e}")
    _record_run(state, True, f"written={len(written)}")
    return {"ok": True, "written": len(written), "ids": written, "dry_run": dry_run}


def main() -> int:
    ap = argparse.ArgumentParser(description="Claude.ai Example 会話 → personal 取り込み (admin, Mac Studio)")
    ap.add_argument("--count", type=int, default=20)
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--no-headless", action="store_true")
    a = ap.parse_args()
    try:
        r = asyncio.run(sync(max_conversations=a.count, dry_run=a.dry_run, headless=not a.no_headless))
    except Exception as e:
        logger.exception("example sync が例外で停止")
        _record_run(load_state(), False, f"exception: {type(e).__name__}: {e}")
        return 1
    print(r)
    return 0 if r.get("ok") else 1


if __name__ == "__main__":
    raise SystemExit(main())
