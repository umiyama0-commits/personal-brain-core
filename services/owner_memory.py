"""services/owner_memory.py — 海山専用の恒久 owner-memory + タスク + リマインダー書込
(★2026-07-20 個人エージェント評価 #1: run_agent が Redis 7日で忘れる穴の根治)

個人アシスタント (run_agent) の会話から恒久的な 事実/嗜好/進行中タスク を抽出し、
plain markdown (§1.19 substrate = repo layout の data/brain/owner_memory/) に永続化する。
クローン側の clone_memory (相手ごと) とは別物 = これは「海山自身について」のメモリ。

防御:
- 話者帰属: 抽出根拠は海山の発話のみ (AI 応答からの逆流入を prompt で明示ブロック、
  dev_journal_sync と同じ原則)
- 揮発情報は保存しない (売上数値/当日の予定 等、2週間で陳腐化するものは skip)
- 抽出は fire-and-forget (応答レイテンシに乗せない)、失敗は §1.18 loud_fail
  (threshold=10, 24h cooldown = 10 turn 連続で抽出が死んだら通知)
- 閲覧/編集は LINE `/memory` (admin gate 済経路) — 「今何を覚えているか」を一望・削除可能

data 配置 (git 非追跡 = Mac Studio runtime 生成、§1.14):
- data/brain/owner_memory/memory.md   … 恒久メモリ (facts/preferences/ongoing)
- data/brain/owner_memory/tasks.md    … タスク checklist (- [ ] / - [x])
- data/brain/reminders/<date>.md      … 既存 clone_reminder_check (09:00 JST push) 互換
"""
from __future__ import annotations

import json
import logging
import os
import re
from datetime import datetime, timedelta, timezone
from pathlib import Path

logger = logging.getLogger("owner_memory")

JST = timezone(timedelta(hours=9))

SECTIONS = {
    "facts": "事実",
    "preferences": "嗜好",
    "ongoing": "進行中",
}
# 進行中はこの日数を超えたら注入から外す (ファイルには残す = /memory では見える)
ONGOING_INJECT_DAYS = 90

_ENTRY_RE = re.compile(r"^- \[(\d{4}-\d{2}-\d{2})\](?: \((auto)\))? (.+)$")
_TASK_RE = re.compile(r"^- \[([ x])\] (?:\[(\d{4}-\d{2}-\d{2})\] )?(.+)$")
_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
# 配送 cron (clone_reminder_check) の実行時刻。これ以降の当日指定は永久未配送になるため拒否
REMINDER_DELIVERY_HOUR = 9


def _base_dir() -> Path:
    return Path(os.getenv("BRAIN_APP_ROOT", "/app")) / "data" / "brain"


def _memory_path() -> Path:
    return _base_dir() / "owner_memory" / "memory.md"


def _tasks_path() -> Path:
    return _base_dir() / "owner_memory" / "tasks.md"


def _reminders_dir() -> Path:
    return _base_dir() / "reminders"


def _now() -> datetime:
    """現在時刻 JST (テストで monkeypatch する単一点)。"""
    return datetime.now(JST)


def _today() -> str:
    return _now().strftime("%Y-%m-%d")


def _normalize(text: str) -> str:
    """dedup 用の正規化 (空白/句読点ゆらぎを吸収)。

    ★cross-check Reviewer 指摘: ASCII '.' ',' は除去しない — 除去すると
    小数/バージョン (10.5% vs 105%、v1.2 vs v12) が同一視され別事実が無音破棄される。
    """
    return re.sub(r"[\s、。!！?？]", "", text or "").lower()


# ─── メモリ read/parse ───

def parse_entries(content: str | None = None) -> list[dict]:
    """memory.md を [{section, date, text}] に parse (表示順 = ファイル順)。"""
    if content is None:
        p = _memory_path()
        content = p.read_text(encoding="utf-8") if p.exists() else ""
    entries: list[dict] = []
    section = ""
    label_to_key = {v: k for k, v in SECTIONS.items()}
    for line in content.splitlines():
        if line.startswith("## "):
            section = label_to_key.get(line[3:].strip(), "")
            continue
        m = _ENTRY_RE.match(line)
        if m and section:
            entries.append({
                "section": section, "date": m.group(1),
                "auto": m.group(2) == "auto", "text": m.group(3),
            })
    return entries


def _write_entries(entries: list[dict]) -> None:
    p = _memory_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    lines = ["# Owner Memory (海山)", ""]
    for key, label in SECTIONS.items():
        lines.append(f"## {label}")
        for e in entries:
            if e["section"] == key:
                marker = " (auto)" if e.get("auto") else ""
                lines.append(f"- [{e['date']}]{marker} {e['text']}")
        lines.append("")
    p.write_text("\n".join(lines), encoding="utf-8")


def add_entry(section: str, text: str, date: str | None = None, auto: bool = False) -> bool:
    """1 件追加。既存と正規化重複なら False。section 不正も False。

    auto=True は会話からの自動抽出 (★cross-check DA: 手動と区別して表示し、
    汚染発覚時に auto 由来を判別できるようにする)。
    """
    text = (text or "").strip()
    if not text or section not in SECTIONS:
        return False
    entries = parse_entries()
    norm = _normalize(text)
    if any(_normalize(e["text"]) == norm for e in entries):
        return False
    entries.append({"section": section, "date": date or _today(), "auto": auto, "text": text})
    _write_entries(entries)
    return True


def remove_entry(index: int) -> str | None:
    """表示番号 (1-based、format_display と同順) で削除。削除した text を返す。"""
    entries = parse_entries()
    ordered = _display_order(entries)
    if not (1 <= index <= len(ordered)):
        return None
    target = ordered[index - 1]
    entries.remove(target)
    _write_entries(entries)
    return target["text"]


def _display_order(entries: list[dict]) -> list[dict]:
    """表示/削除番号の正準順 = section 順 (facts→preferences→ongoing) → ファイル順。"""
    ordered = []
    for key in SECTIONS:
        ordered.extend(e for e in entries if e["section"] == key)
    return ordered


def format_display() -> str:
    """LINE `/memory` 用の一覧 (番号付き = `/memory del N` で削除)。"""
    entries = parse_entries()
    ordered = _display_order(entries)
    if not ordered and not open_tasks() and not pending_reminders():
        return "🧠 Owner Memory は空です。会話から自動蓄積されるほか、/memory add <内容> で手動追加できます。"
    out = ["🧠 Owner Memory (海山)"]
    i = 0
    for key, label in SECTIONS.items():
        sec = [e for e in ordered if e["section"] == key]
        if sec:
            out.append(f"\n■ {label}")
            for e in sec:
                i += 1
                marker = " ⚙" if e.get("auto") else ""
                out.append(f"{i}. [{e['date']}]{marker} {e['text']}")
    tasks = open_tasks()
    if tasks:
        out.append("\n■ タスク (未完了)")
        out.extend(f"・{t}" for t in tasks)
    pend = pending_reminders()
    if pend:
        out.append("\n■ リマインダー (配信待ち)")
        out.extend(f"・{p}" for p in pend)
    out.append("\n⚙=会話から自動抽出。操作: /memory del <番号> | /memory add <内容>")
    return "\n".join(out)


def load_memory_block(max_chars: int = 1600) -> str:
    """system prompt 注入用ブロック。空なら ''。進行中は 90 日以内のみ。"""
    entries = parse_entries()
    cutoff = (datetime.now(JST) - timedelta(days=ONGOING_INJECT_DAYS)).strftime("%Y-%m-%d")
    lines: list[str] = []
    for key, label in SECTIONS.items():
        sec = [
            e for e in entries
            if e["section"] == key and (key != "ongoing" or e["date"] >= cutoff)
        ]
        if sec:
            lines.append(f"[{label}]")
            # 新しいものを優先して残すため逆順で詰める
            lines.extend(f"- {e['text']} ({e['date']})" for e in reversed(sec))
    tasks = open_tasks()
    if tasks:
        lines.append("[未完了タスク]")
        lines.extend(f"- {t}" for t in tasks[:10])
    if not lines:
        return ""
    block = "\n".join(lines)
    if len(block) > max_chars:
        block = block[:max_chars] + "\n…(以下省略。全量は /memory)"
    return block


# ─── タスク ───

def _read_tasks() -> list[dict]:
    p = _tasks_path()
    if not p.exists():
        return []
    tasks = []
    for line in p.read_text(encoding="utf-8").splitlines():
        m = _TASK_RE.match(line)
        if m:
            tasks.append({"done": m.group(1) == "x", "date": m.group(2) or "", "text": m.group(3)})
    return tasks


def _write_tasks(tasks: list[dict]) -> None:
    p = _tasks_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    lines = ["# Tasks (海山)", ""]
    for t in tasks:
        mark = "x" if t["done"] else " "
        date = f"[{t['date']}] " if t["date"] else ""
        lines.append(f"- [{mark}] {date}{t['text']}")
    p.write_text("\n".join(lines) + "\n", encoding="utf-8")


def add_task(text: str) -> bool:
    text = (text or "").strip()
    if not text:
        return False
    tasks = _read_tasks()
    norm = _normalize(text)
    if any(not t["done"] and _normalize(t["text"]) == norm for t in tasks):
        return False
    tasks.append({"done": False, "date": _today(), "text": text})
    _write_tasks(tasks)
    return True


def complete_task(match: str) -> str | None:
    """部分一致で最初の未完了タスクを完了化。完了した text を返す。"""
    match = (match or "").strip()
    if not match:
        return None
    tasks = _read_tasks()
    norm = _normalize(match)
    if not norm:  # ★cross-check Reviewer: 正規化後空 ('' in x は常に True) で先頭誤完了を防ぐ
        return None
    for t in tasks:
        if not t["done"] and (norm in _normalize(t["text"]) or _normalize(t["text"]) in norm):
            t["done"] = True
            _write_tasks(tasks)
            return t["text"]
    return None


def open_tasks() -> list[str]:
    return [f"{t['text']} ({t['date']})" if t["date"] else t["text"] for t in _read_tasks() if not t["done"]]


# ─── リマインダー (既存 clone_reminder_check 互換 = 指定日の 09:00 JST に LINE Push) ───
# ★cross-check DA: bot 自動生成は git 追跡の reminders/ 直下でなく非追跡の auto/ に書く
# (MacBook 手動 commit との pull 衝突 + private 内容が追跡 path に乗る漏洩面を回避)。
# 配送側 clone_reminder_check は auto/<date>.md も見る (同時改修)。


def create_reminder(date_str: str, title: str, body: str = "") -> str:
    """data/brain/reminders/auto/<date>.md を作成/追記。戻り値はユーザ向け確認文。"""
    date_str = (date_str or "").strip()
    title = (title or "").strip()
    # ★cross-check Reviewer: 形式だけでなく実在日付を検証 (2026-13-45 が通ると永久未配送)
    if not _DATE_RE.match(date_str):
        return "エラー: 日付は YYYY-MM-DD 形式で指定してください。"
    try:
        datetime.strptime(date_str, "%Y-%m-%d")
    except ValueError:
        return f"エラー: {date_str} は実在しない日付です。"
    now = _now()
    if date_str < _today():
        return f"エラー: {date_str} は過去日です (今日は {_today()})。"
    # ★cross-check 3体一致: 当日 09:00 以降は配送 cron 通過済 = 受理すると「届きます」が虚偽になる
    if date_str == _today() and now.hour >= REMINDER_DELIVERY_HOUR:
        return (f"エラー: 本日 {REMINDER_DELIVERY_HOUR}:00 の配信時刻を過ぎています。"
                "翌日以降の日付を指定してください。")
    if not title:
        return "エラー: リマインダーのタイトルが空です。"
    d = _reminders_dir() / "auto"
    d.mkdir(parents=True, exist_ok=True)
    path = d / f"{date_str}.md"
    entry = f"# {title}\n\n{body.strip()}\n" if body.strip() else f"# {title}\n"
    if path.exists():
        existing = path.read_text(encoding="utf-8")
        if _normalize(title) and _normalize(title) in _normalize(existing):
            return f"既に同じリマインダーが {date_str} に設定済みです — {title}"
        path.write_text(existing.rstrip() + "\n\n---\n\n" + entry, encoding="utf-8")
    else:
        path.write_text(entry, encoding="utf-8")
    return f"⏰ リマインダーを設定: {date_str} の朝 9:00 に LINE で届きます — {title}"


def pending_reminders() -> list[str]:
    """配信待ちリマインダーの一覧 (手動 + auto、_sent 除く)。「日付: タイトル」形式。"""
    out = []
    for d in (_reminders_dir(), _reminders_dir() / "auto"):
        if not d.is_dir():
            continue
        for p in sorted(d.glob("*.md")):
            if p.stem < _today():
                continue  # 配信済/期限切れは出さない
            try:
                first = p.read_text(encoding="utf-8").splitlines()[0]
            except Exception:
                continue
            title = first.lstrip("# ").strip() or "(無題)"
            out.append(f"{p.stem}: {title}")
    return out[:10]


# ─── 会話からの自動抽出 (fire-and-forget) ───

EXTRACT_PROMPT = """あなたは秘書のメモリー係。以下は海山 (OWNDAYS CEO) と彼の AI アシスタントの 1 ターンの会話。
海山の**発話のみ**を根拠に、長期 (2週間以上) 価値が残る恒久情報だけを抽出せよ。

抽出対象:
- facts: 恒久的な事実 (例: 定宿、家族構成の言及、健康上の恒常的な留意点)
- preferences: 嗜好・やり方の好み (例: 資料は結論先出しが好み)
- ongoing: 進行中の案件・予定している事柄 (例: ◯◯の件を△△と進めている)

除外 (絶対に抽出しない):
- AI の応答にしか出てこない情報 (海山が言っていないこと)
- 売上・KPI 等の数値 (日々変わる)、当日限りの予定、単発の質問内容
- 既存メモリと同内容 (下の既存一覧と重複するもの)
- 挨拶・雑談・感想のみのターン

既存メモリ:
{existing}

会話:
海山: {user}
AI: {reply}

JSON のみで出力: {{"items": [{{"section": "facts|preferences|ongoing", "text": "60字以内"}}]}}
該当なしなら {{"items": []}}。最大 3 件。"""


def enabled() -> bool:
    return os.getenv("OWNER_MEMORY_ENABLED", "1") == "1"


async def extract_from_turn(http, user_message: str, reply: str, litellm_url: str, litellm_key: str) -> int:
    """1 ターンから恒久情報を抽出して保存。保存件数を返す。"""
    existing = "\n".join(f"- {e['text']}" for e in parse_entries()[-40:]) or "(なし)"
    model = os.getenv("OWNER_MEMORY_MODEL", "fast")  # §1.19 alias、hardcode しない
    resp = await http.post(
        f"{litellm_url}/v1/chat/completions",
        headers={"Authorization": f"Bearer {litellm_key}"},
        json={
            "model": model,
            "messages": [{
                "role": "user",
                "content": EXTRACT_PROMPT.format(
                    existing=existing[:2000],
                    user=(user_message or "")[:1500],
                    reply=(reply or "")[:800],
                ),
            }],
            "max_tokens": 300,
            "temperature": 0,
        },
        timeout=30.0,
    )
    resp.raise_for_status()
    content = resp.json()["choices"][0]["message"]["content"] or ""
    m = re.search(r"\{.*\}", content, re.DOTALL)
    if not m:
        return 0
    items = json.loads(m.group(0)).get("items", [])
    saved: list[str] = []
    for item in items[:3]:
        if isinstance(item, dict) and add_entry(item.get("section", ""), item.get("text", ""), auto=True):
            saved.append(item.get("text", ""))
    # ★cross-check DA: auto 保存は海山に 1 行通知して「気付ける」を担保 (誤事実の即削除導線)
    if saved and os.getenv("OWNER_MEMORY_NOTIFY", "1") == "1":
        try:
            import asyncio
            _load_scripts_path()
            from clone_improve_lib import line_push_digest
            note = "🧠 会話から記憶しました (誤りは /memory del で削除):\n" + "\n".join(f"・{t}" for t in saved)
            # ★2026-07-20 通知削減: 即時 push でなく 1日2回のまとめに合流
            await asyncio.to_thread(line_push_digest, note, "記憶")
        except Exception as e:
            logger.warning(f"owner_memory notify failed: {e}")
    return len(saved)


def _load_scripts_path() -> None:
    import sys
    _scripts = str(Path(__file__).resolve().parents[1] / "scripts")
    if _scripts not in sys.path:
        sys.path.insert(0, _scripts)


async def post_turn(http, user_message: str, reply: str, litellm_url: str, litellm_key: str) -> None:
    """run_agent 応答後の fire-and-forget hook。失敗は握りつぶさず streak 記録 (§1.18)。"""
    if not enabled():
        return
    import asyncio
    ok = True
    try:
        n = await extract_from_turn(http, user_message, reply, litellm_url, litellm_key)
        if n:
            logger.info(f"owner_memory: {n} 件保存")
    except Exception as e:
        ok = False
        logger.warning(f"owner_memory extract failed: {type(e).__name__}: {e}")
    try:
        _load_scripts_path()
        from clone_improve_lib import loud_fail
        # ★cross-check Reviewer: fcntl file IO + 通知 HTTP は event loop を塞ぐため to_thread
        await asyncio.to_thread(
            loud_fail, "owner_memory_extract", ok,
            detail="run_agent post-turn 抽出", threshold=10, cooldown_h=24,
        )
    except Exception:
        pass  # loud_fail 自体の失敗で本処理を壊さない


_bg_tasks: set = set()


def spawn_post_turn(http, user_message: str, reply: str, litellm_url: str, litellm_key: str) -> None:
    """post_turn を fire-and-forget 起動。★cross-check: task 参照を保持し GC 消失を防ぐ
    (asyncio は task を弱参照でしか持たない = 参照を捨てると実行途中で GC され得る)。"""
    import asyncio
    try:
        t = asyncio.create_task(post_turn(http, user_message, reply, litellm_url, litellm_key))
        _bg_tasks.add(t)
        t.add_done_callback(_bg_tasks.discard)
    except Exception as e:
        logger.warning(f"owner_memory spawn failed: {e}")


# ─── LINE /memory コマンド (brain_commands から呼ぶ) ───

def handle_memory_command(message: str) -> str | None:
    """`/memory` 系を処理。非該当は None (他コマンドへフォールスルー)。"""
    text = (message or "").strip()
    if text == "/memory":
        return format_display()
    if text.startswith("/memory del "):
        arg = text[len("/memory del "):].strip()
        if not arg.isdigit():
            return "使い方: /memory del <番号> (番号は /memory の一覧参照)"
        removed = remove_entry(int(arg))
        return f"🗑 削除: {removed}" if removed else f"番号 {arg} が見つかりません (/memory で確認)"
    if text.startswith("/memory add "):
        content = text[len("/memory add "):].strip()
        if add_entry("facts", content):
            return f"🧠 記憶しました: {content}"
        return "既に同内容の記憶があります (/memory で確認)"
    if text.startswith("/memory"):
        return "使い方: /memory | /memory add <内容> | /memory del <番号>"
    return None
