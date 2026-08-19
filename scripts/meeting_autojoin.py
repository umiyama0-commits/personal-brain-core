#!/usr/bin/env python3
"""meeting_autojoin.py — umiyama の web 会議へ Recall bot を自動参加させ議事録→wiki

★2026-07-03 海山指示「ceo@owndays.co.jp が参加する web mtg に umiyama-ai を自動参加させて
議事録を取って、Plaud と同様に wiki に追加するようにしたい」。

アーキテクチャ (polling-first):
  cron (10分毎 7-22時) → ①Google Calendar (ceo@owndays.co.jp、共有済) から先 36h の
  予定を取得 → ②参加判定 (会議URL有 + denylist/社外/辞退/終日 除外) → ③Recall.ai bot を
  join_at 付きで予約 (state で冪等、時刻変更は作り直し、キャンセルは bot 削除) →
  ④終了した bot の transcript を poll で取得 → ⑤既存 /api/meeting/ingest へ POST
  (= Plaud と同一経路: PrivacyGate → compile_meeting_note → wiki/meetings/) → LINE 通知。

  webhook (/webhook/recall) は補助 (未実証のまま残置)。取得の主経路は poll =
  webhook 配信設定や payload 形状の変化に依存しない。

録音除外 (docs/integrations/meeting-pipeline.md の policy):
  人事評価/考課/面接/面談/1on1/M&A/弁護士/税理士/会計士/医療/プライベート 等は
  title/description の denylist で skip。社外参加者が居る会議は default skip
  (MEETING_AUTOJOIN_ALLOW_EXTERNAL=1 で解除)。title に [no-ai] で個別 opt-out。

env:
  MEETING_AUTOJOIN_ENABLED=1        # opt-out gate
  MEETING_TARGET_CALENDAR=ceo@owndays.co.jp
  RECALL_API_KEY / RECALL_API_BASE (ap-northeast-1 実証済)
  MEETING_AUTOJOIN_ALLOW_EXTERNAL=0 # 1 で社外同席会議も参加
  MEETING_DENYLIST_EXTRA=           # 追加 denylist regex
  MEETING_BOT_NAME=Take Umiyama AI

実行: python3 scripts/meeting_autojoin.py [--dry-run] [--horizon-hours 36]
cron: scripts/meeting_autojoin_cron.sh (mkdir lock + cron_env)
"""
from __future__ import annotations

import argparse
import json
import logging
import os
import re
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import httpx

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "scripts"))

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger("meeting_autojoin")

JST = timezone(timedelta(hours=9))
STATE_FILE = ROOT / "data" / "brain" / ".meeting_autojoin_state.json"

RECALL_BASE = os.getenv("RECALL_API_BASE", "https://ap-northeast-1.recall.ai").rstrip("/")
TARGET_CALENDAR = os.getenv("MEETING_TARGET_CALENDAR", "ceo@owndays.co.jp")
BOT_NAME = os.getenv("MEETING_BOT_NAME", "Take Umiyama AI")  # ★2026-07-14 海山指示で改名
# 「社内」ドメイン (dry-run 実測: owndays.com = グローバル側も社員が使う)。
# 親会社 lenskart.com は「社外パートナー」境界の判断が要るため default 非許可
# (足すなら .env で MEETING_ALLOWED_DOMAINS=owndays.co.jp,owndays.com,lenskart.com)
ALLOWED_DOMAINS = {
    d.strip().lower() for d in os.getenv(
        "MEETING_ALLOWED_DOMAINS", "owndays.co.jp,owndays.com").split(",") if d.strip()
}

# ─── 参加判定 (pure) ─────────────────────────────────────
# 録音除外 policy (meeting-pipeline.md) + 実カレンダー実例 (「CFO面接」)。
# 迷ったら block (fail-safe)。個別解除は title から keyword を外すか [ai-ok] を付ける。
DENYLIST_RE = re.compile(
    r"(人事評価|考課|査定|処遇|昇給|昇格|昇進|昇任|面接|面談|1\s*on\s*1|1on1|1\s*[:：]\s*1|"
    # ★DA R2 + reviewer SF-4: 実カレンダーは日英混在 — 個別処遇・雇用・機密系を拡張。
    # bare "promotion"/"performance" は販促/マーケ定例を大量誤 block するため
    # HR 文脈の複合語のみ (performance review / comp review 等)。
    r"退職|解雇|リストラ|内定|オファー|労務|給与|報酬|賞与|"
    r"極秘|組織再編|株式譲渡|資本提携|出資|"
    r"M&A|買収|デューデリ|due\s*diligence|"
    r"弁護士|税理士|会計士|監査法人|内部監査|法律相談|"
    r"医療|病院|診察|検診|健康診断|カウンセリング|"
    r"ハラスメント|相談窓口|通報|懲戒|"
    r"プライベート|私用|家族|"
    r"\[no-?ai\]|録音\s*(NG|禁止|不可)|interview|appraisal|salar(y|ies)|"
    r"\bcomp(ensation)?\s*(review|discussion)?\b|performance\s*review|"
    r"\boffer\b|termination|layoff|severance|PIP\b)",
    re.IGNORECASE,
)
ALLOW_MARKER_RE = re.compile(r"\[ai-?ok\]", re.IGNORECASE)

MEET_URL_RE = re.compile(
    r"https://(?:meet\.google\.com/[a-z0-9\-]+"
    r"|[\w.-]*zoom\.us/j/[\w?=&\-]+"
    r"|teams\.microsoft\.com/l/meetup-join/[^\s<>\"']+)",
    re.IGNORECASE,
)


def _parse_dt(s: str) -> datetime:
    """RFC3339 → datetime。host cron は python 3.9 で 'Z' suffix を fromisoformat が
    受けない (3.11+ のみ) ため置換 (Google は通常 offset 形式だが UTC event は Z があり得る)。"""
    return datetime.fromisoformat((s or "").replace("Z", "+00:00"))


def extract_meeting_url(event: dict) -> str:
    """event から web 会議 URL を取り出す (Meet 優先 → conferenceData → 本文 regex)。"""
    if event.get("hangoutLink"):
        return event["hangoutLink"]
    for ep in (event.get("conferenceData") or {}).get("entryPoints", []) or []:
        if ep.get("entryPointType") == "video" and ep.get("uri"):
            return ep["uri"]
    for field in ("location", "description"):
        m = MEET_URL_RE.search(event.get(field) or "")
        if m:
            return m.group(0)
    return ""


def should_join(event: dict, now: datetime, allow_external: bool = False,
                extra_deny: str = "") -> tuple[bool, str]:
    """参加すべきか (ok, reason)。reason は skip 理由 or 'ok'。fail-safe = 迷ったら skip。"""
    status = event.get("status", "confirmed")
    if status == "cancelled":
        return False, "cancelled"
    start_raw = (event.get("start") or {}).get("dateTime")
    if not start_raw:
        return False, "all_day"  # 終日イベントは会議でない
    end_raw = (event.get("end") or {}).get("dateTime") or start_raw
    start = _parse_dt(start_raw)
    end = _parse_dt(end_raw)
    if end <= now:
        return False, "ended"
    if (end - start) > timedelta(hours=4):
        return False, "too_long"
    url = extract_meeting_url(event)
    if not url:
        return False, "no_video_url"

    text = f"{event.get('summary') or ''} {event.get('description') or ''}"
    # [ai-ok] = 海山の明示 opt-in。denylist と参加者系 skip (solo/no_attendees/external/
    # two_person) を上書きする (informed consent)。cancelled/ended/URL無し等の物理条件は不可。
    # ★DA R5: marker を書けるのは event の organizer — 社外 organizer が [ai-ok] を書いて
    # bot を社外会議に引き込めるため、organizer が社内 (ALLOWED_DOMAINS) の時のみ信用する。
    _org = ((event.get("organizer") or {}).get("email") or "").lower()
    _org_trusted = (_org.split("@")[-1] in ALLOWED_DOMAINS) if _org else False
    ai_ok = bool(ALLOW_MARKER_RE.search(text)) and _org_trusted
    if not ai_ok:
        if DENYLIST_RE.search(text):
            return False, "denylist"
        if extra_deny:
            try:
                if re.search(extra_deny, text, re.IGNORECASE):
                    return False, "denylist_extra"
            except re.error:
                pass  # 壊れた extra regex で全 skip にしない (基本 denylist は適用済)

    attendees = event.get("attendees") or []
    humans = [a for a in attendees if not a.get("resource")
              and "resource.calendar.google.com" not in (a.get("email") or "")]
    # 本人が辞退した会議には送らない ([ai-ok] でも辞退会議には行かない)
    for a in humans:
        if a.get("self") or (a.get("email") or "").lower() == TARGET_CALENDAR.lower():
            if a.get("responseStatus") == "declined":
                return False, "declined"
    if not ai_ok:
        # attendees 不明 = 誰が居るか分からない会議 = fail-safe skip
        # (手入力 event 等で誤 skip なら title に [ai-ok] で参加)
        if not humans:
            return False, "no_attendees"
        # 1人会議 (自分メモ等) は skip
        if len(humans) < 2:
            return False, "solo"
        # ★DA R1 (実カレンダー実証: join 8件中3件が「Catchup」「X<>Y」型の実質1on1):
        # 2人会議 = de-facto 1on1 = policy の除外対象。命名規則 (英語 Catchup 等) は
        # regex で追い切れないため人数で判定。参加したい 1on1 は [ai-ok] で opt-in。
        if len(humans) == 2:
            return False, "two_person"
        # 社外同席は default skip (取引先/社外パートナー除外 policy)
        if not allow_external:
            for a in humans:
                email = (a.get("email") or "").lower()
                if email and email.split("@")[-1] not in ALLOWED_DOMAINS:
                    return False, f"external:{email.split('@')[-1]}"
    return True, "ok"


def build_transcript_text(data) -> tuple[str, list[str]]:
    """Recall transcript (新旧 API どちらの形でも) → 'speaker: text' 行 + 参加者一覧。

    許容する形:
      [{"speaker": s, "words": [{"text": ...}]}, ...]                    (legacy)
      [{"participant": {"name": s}, "words": [{"text": ...}]}, ...]      (新)
    """
    lines: list[str] = []
    speakers: list[str] = []
    if isinstance(data, dict):
        data = data.get("results") or data.get("data") or []
    for seg in data or []:
        if not isinstance(seg, dict):
            continue
        sp = (seg.get("speaker")
              or (seg.get("participant") or {}).get("name")
              or "不明")
        words = seg.get("words") or []
        text = " ".join(
            w.get("text", "") for w in words if isinstance(w, dict)
        ).strip() or (seg.get("text") or "").strip()
        if not text:
            continue
        if sp not in speakers:
            speakers.append(sp)
        lines.append(f"{sp}: {text}")
    return "\n".join(lines), speakers


# sub_code → 海山が読んで**次に何をすればいいか**分かる説明 (★2026-08-03)
_FATAL_HINT = {
    "timeout_exceeded_waiting_room":
        "待機室に入ったまま誰も入室許可せず終了。Meet の許可 UI は "
        "カレンダーの主催者/共同主催者にしか出ないため、主催者が実出席者でない定例だと"
        "誰も気付けない。→ 共同主催者を事前指名 (カレンダーUI) が最も手軽",
    "bot_kicked_from_waiting_room": "待機室から削除された (参加者が拒否)",
    "google_meet_bot_blocked":
        "参加リクエスト自体が拒否された。会議の「リンクを知っている人は参加をリクエストできる」"
        "が外れている可能性",
    "timeout_exceeded_everyone_left": "会議に誰も来なかった / 全員退出",
}


def _notify_fatal(entry: dict, code: str) -> None:
    """★DA R8: 議事録が取れなかった会議を silent にしない (「あるはず」期待とのズレ防止)。
    ★2026-08-03: sub_code だけだと何をすべきか分からないため、対処のヒントを添える
    (実測で 8/12 が待機室 timeout = 同じ対処で一括して直る種類の失敗だった)。"""
    try:
        from clone_improve_lib import line_push, line_push_digest
        hint = _FATAL_HINT.get(code)
        msg = f"⚠️ 議事録取れず: {entry.get('title', '?')} ({code})"
        if hint:
            msg += f"\n→ {hint}"
        line_push_digest(msg, "会議")
    except Exception:
        pass


# ─── state ───────────────────────────────────────────────
def load_state() -> dict:
    if not STATE_FILE.exists():
        return {"events": {}}
    try:
        return json.loads(STATE_FILE.read_text(encoding="utf-8"))
    except Exception as e:
        # ★DA R7: 破損を silent に空 state 化すると「二重 bot + 回収漏れ」— 証拠保全 + loud log
        backup = STATE_FILE.with_suffix(f".corrupt-{datetime.now(JST).strftime('%m%d%H%M')}")
        try:
            STATE_FILE.rename(backup)
        except Exception:
            pass
        logger.error(f"state 破損 → {backup.name} に退避して空から再構築 "
                     f"(reconcile が orphan bot を回収): {e}")
        return {"events": {}, "_corrupt_recovered": True}


def save_state(st: dict) -> None:
    st["updated"] = datetime.now(JST).isoformat(timespec="seconds")
    STATE_FILE.write_text(json.dumps(st, ensure_ascii=False, indent=2), encoding="utf-8")


# ─── Recall API ──────────────────────────────────────────
def _recall_headers() -> dict:
    return {"Authorization": f"Token {os.getenv('RECALL_API_KEY', '')}",
            "Content-Type": "application/json"}


def create_bot(http: httpx.Client, meeting_url: str, join_at: datetime,
               title: str) -> dict:
    """Recall bot を予約。新 schema (recording_config) → 400 なら legacy に fallback。"""
    base_payload = {
        "meeting_url": meeting_url,
        "bot_name": BOT_NAME,
        "join_at": join_at.astimezone(timezone.utc).isoformat().replace("+00:00", "Z"),
        "metadata": {"title": title[:100], "source": "meeting_autojoin"},
        # ★2026-08-03: 待機室の滞留時間を既定 1200s → 300s に短縮。
        #   実測 (Recall sub_code) で失敗 12 件中 **8 件が timeout_exceeded_waiting_room** =
        #   「待機室に入ったまま誰も許可せず 20 分待って退出」だった。Google Meet の Host
        #   Controls 下では待機室の許可 UI が **カレンダー organizer と共同主催者にしか出ない**
        #   ため、organizer が実出席者でない定例では誰も気付けない (公式トラブルシュート記載)。
        #   短縮しても入室できるようにはならないが、入れない会議で 20 分居座る無駄を削り、
        #   失敗の検知も早くなる。env で調整可。
        "automatic_leave": {
            "waiting_room_timeout": int(os.getenv("RECALL_WAITING_ROOM_TIMEOUT", "300")),
        },
    }
    # ★Signed-In Bot (待機室スキップの唯一の公式解): Google Login Group を用意した場合のみ有効。
    #   未設定なら従来どおり匿名 bot で動く (後方互換)。有効化には bot 専用の別ドメイン
    #   Workspace + SAML/SSO 設定が必要 = 海山/IT の決裁事項のため、env 注入で切替可能にしておく。
    _login_group = os.getenv("RECALL_GOOGLE_LOGIN_GROUP_ID", "").strip()
    if _login_group:
        base_payload["google_meet"] = {
            "login_required": True,
            "google_login_group_id": _login_group,
        }
    new_style = dict(base_payload)
    new_style["recording_config"] = {
        # ★fact-checker FLAG1: caption 言語は default 英語で auto-detect されない。
        # 日本語会議が英語字幕でゴミ化するため明示 (env で上書き可、Meet は bot 側が言語を設定)。
        "transcript": {"provider": {"meeting_captions": {
            "language_code": os.getenv("MEETING_CAPTION_LANG", "ja"),
        }}},
    }
    r = http.post(f"{RECALL_BASE}/api/v1/bot/", headers=_recall_headers(),
                  json=new_style, timeout=30)
    if r.status_code == 400 and "recording_config" in r.text:
        legacy = dict(base_payload)
        legacy["transcription_options"] = {"provider": "meeting_captions"}
        r = http.post(f"{RECALL_BASE}/api/v1/bot/", headers=_recall_headers(),
                      json=legacy, timeout=30)
    r.raise_for_status()
    return r.json()


def delete_bot(http: httpx.Client, bot_id: str) -> bool:
    r = http.delete(f"{RECALL_BASE}/api/v1/bot/{bot_id}/",
                    headers=_recall_headers(), timeout=30)
    if r.status_code in (200, 204, 404):
        return True
    # ★reviewer N-5: DELETE は未参加 bot 専用 — 参加中 (denylist/[no-ai] が会議中に付いた等)
    # は leave_call で退室させる (privacy: その場に残さない)
    r2 = http.post(f"{RECALL_BASE}/api/v1/bot/{bot_id}/leave_call/",
                   headers=_recall_headers(), timeout=30)
    return r2.status_code in (200, 204, 404)


def delete_bot_media(http: httpx.Client, bot_id: str) -> bool:
    """★DA R4: ingest 成功後に Recall 側の録音/transcript を削除 (第三者クラウドに
    社内会議の音声を無期限保持しない)。失敗しても ingest は成立済 = 非致命。"""
    try:
        r = http.post(f"{RECALL_BASE}/api/v1/bot/{bot_id}/delete_media/",
                      headers=_recall_headers(), timeout=30)
        return r.status_code in (200, 204)
    except Exception:
        return False


def get_bot(http: httpx.Client, bot_id: str) -> dict:
    r = http.get(f"{RECALL_BASE}/api/v1/bot/{bot_id}/",
                 headers=_recall_headers(), timeout=30)
    r.raise_for_status()
    return r.json()


def fetch_transcript(http: httpx.Client, bot: dict) -> list:
    """transcript を取得 (legacy endpoint → 新 media_shortcuts の順で試す)。"""
    bot_id = bot.get("id", "")
    r = http.get(f"{RECALL_BASE}/api/v1/bot/{bot_id}/transcript/",
                 headers=_recall_headers(), timeout=60)
    if r.status_code == 200:
        return r.json()
    # 新 API: recordings[].media_shortcuts.transcript.data.download_url
    for rec in bot.get("recordings") or []:
        url = (((rec.get("media_shortcuts") or {}).get("transcript") or {})
               .get("data") or {}).get("download_url")
        if url:
            r2 = http.get(url, timeout=60)
            if r2.status_code == 200:
                return r2.json()
    raise RuntimeError(f"transcript unavailable for bot {bot_id} (status {r.status_code})")


def reconcile_orphan_bots(http: httpx.Client, st: dict) -> int:
    """★DA R7: state に無い自作 bot (crash/破損で bot_id 消失) を Recall 側から発見して掃除。

    metadata.source==meeting_autojoin (create 時に必ず付与) かつ 未参加 (join_at 未来) の
    bot だけ削除。参加済み orphan は削除せず warn (録音が存在する = 人が判断)。"""
    known = {e.get("bot_id") for e in st.get("events", {}).values() if e.get("bot_id")}
    removed = 0
    try:
        r = http.get(f"{RECALL_BASE}/api/v1/bot/?limit=100",
                     headers=_recall_headers(), timeout=30)
        r.raise_for_status()
        for b in (r.json().get("results") or []):
            bid = b.get("id", "")
            meta = b.get("metadata") or {}
            if meta.get("source") != "meeting_autojoin" or bid in known:
                continue
            join_at = b.get("join_at") or ""
            try:
                future = _parse_dt(join_at) > datetime.now(timezone.utc)
            except Exception:
                future = False
            if future:
                logger.warning(f"orphan bot (state 外・未参加) → 削除: {bid}")
                delete_bot(http, bid)
                removed += 1
            else:
                logger.warning(f"orphan bot (state 外・参加済み?) 発見 {bid} — "
                               "録音が Recall に残っている可能性。手動確認を")
    except Exception as e:
        logger.warning(f"reconcile 失敗 (継続): {e}")
    return removed


def bot_status(bot: dict) -> str:
    """bot detail から最新 status code を返す。"""
    changes = bot.get("status_changes") or []
    if changes:
        return (changes[-1] or {}).get("code", "")
    return (bot.get("status") or {}).get("code", "") if isinstance(bot.get("status"), dict) else str(bot.get("status") or "")


def bot_failure_reason(bot: dict) -> str:
    """★2026-08-03: 失敗の**真因**を Recall の sub_code から取る。

    実測 (失敗 12 件) の内訳は
      timeout_exceeded_waiting_room 8 / bot_kicked_from_call 2 /
      timeout_exceeded_everyone_left 1 / bot_received_leave_call 1
    で、**大半が「待機室に入ったまま誰も許可せず退出」**だった。従来は最終 code が `done` に
    なるため「正常終了したのに議事録が無い」ようにしか見えず、22 件中 0 件という結果の理由が
    表に出なかった。sub_code を拾えば原因が一意に決まる (Google Meet の Host Controls 下では
    待機室の許可 UI が organizer と共同主催者にしか出ないため、organizer が実出席者でない
    定例では誰も気付けない = 公式トラブルシュート記載の典型例)。
    戻り値は sub_code (無ければ空文字)。
    """
    for s in reversed(bot.get("status_changes") or []):
        sub = (s or {}).get("sub_code")
        if sub:
            return str(sub)
    return ""


# 録音が取れない = 議事録にならない sub_code (通知して原因を可視化する)
NO_RECORDING_SUBCODES = {
    "timeout_exceeded_waiting_room",   # 待機室で放置 → 入室できず
    "bot_kicked_from_waiting_room",    # 待機室から removed
    "google_meet_bot_blocked",         # 参加リクエスト自体が拒否される設定
    "timeout_exceeded_everyone_left",  # 誰も来なかった / 全員退出
}


def _send_daily_digest(events: list, now: datetime, allow_external: bool,
                       extra_deny: str) -> None:
    """当日分の join/skip 予定一覧を LINE push (★DA R8 pre-flight review)。"""
    today = now.astimezone(JST).date().isoformat()
    joins, skips = [], []
    for ev in events:
        start_raw = (ev.get("start") or {}).get("dateTime", "")
        if not start_raw.startswith(today):
            continue
        hm = start_raw[11:16]
        title = (ev.get("summary") or "(無題)")[:38]
        ok, reason = should_join(ev, now, allow_external, extra_deny)
        (joins if ok else skips).append(
            f"{'✅' if ok else '—'} {hm} {title}" + ("" if ok else f" ({reason})"))
    if not joins and not skips:
        return
    try:
        from clone_improve_lib import line_push, line_push_digest
        msg = ["🤖 本日の議事録 bot 予定 (参加は入室許可が要ります)"]
        msg += joins or ["(参加予定なし)"]
        if skips:
            msg.append("--- skip ---")
            msg += skips[:10]
        msg.append("※参加を止める: 予定 title に [no-ai] / 参加させる: [ai-ok]")
        line_push("\n".join(msg)[:3800])  # pre-flight = [no-ai] 拒否権 window、即時必須 (cross-check 3体一致で digest 化を差し戻し)
    except Exception:
        pass


# ─── main cycle ──────────────────────────────────────────
def run_cycle(dry_run: bool = False, horizon_hours: int = 36) -> dict:
    from google_sync import get_credentials
    from googleapiclient.discovery import build

    now = datetime.now(JST)
    allow_external = os.getenv("MEETING_AUTOJOIN_ALLOW_EXTERNAL", "0") == "1"
    extra_deny = os.getenv("MEETING_DENYLIST_EXTRA", "")
    st = load_state()
    st.setdefault("events", {})
    summary = {"scheduled": 0, "rescheduled": 0, "cancelled": 0,
               "ingested": 0, "skipped": {}, "errors": 0}

    svc = build("calendar", "v3", credentials=get_credentials())
    # ★reviewer SF-5: pagination (100件超の window で末尾が漏れると ①.5 が正規 bot を
    # 「event_gone」誤削除するため、全 page 取得。安全上限 500)
    events: list = []
    page_token = None
    listing_complete = True
    while True:
        resp = svc.events().list(
            calendarId=TARGET_CALENDAR,
            timeMin=(now - timedelta(minutes=15)).isoformat(),
            timeMax=(now + timedelta(hours=horizon_hours)).isoformat(),
            singleEvents=True, orderBy="startTime", maxResults=100,
            showDeleted=True, pageToken=page_token,
        ).execute()
        events.extend(resp.get("items", []))
        page_token = resp.get("nextPageToken")
        if not page_token:
            break
        if len(events) >= 500:
            listing_complete = False  # 異常な件数 = ①.5 の消滅判定を skip
            break
    seen_ids = set()
    if st.pop("_corrupt_recovered", False):
        summary["errors"] += 1  # 破損回復は要注意イベントとして計上

    with httpx.Client() as http:
        # ⓪ ★DA R7: state に無い自作 bot の掃除 (crash/state 破損からの自己回復)
        if not dry_run:
            reconcile_orphan_bots(http, st)

        # ★DA R8: 朝 07:0x の cycle で当日の join/skip 予定を海山へ事前 digest
        # (= pre-flight 人間レビュー: 変な join 予定に [no-ai] を付ければ次 cycle で取消)
        if not dry_run and now.hour == 7 and now.minute < 10:
            _send_daily_digest(events, now, allow_external, extra_deny)

        # ① 予約 / 変更追随 / キャンセル
        for ev in events:
            eid = ev.get("id", "")
            seen_ids.add(eid)
            ok, reason = should_join(ev, now, allow_external, extra_deny)
            entry = st["events"].get(eid)
            start_raw = (ev.get("start") or {}).get("dateTime", "")

            if not ok:
                summary["skipped"][reason] = summary["skipped"].get(reason, 0) + 1
                # ★reviewer BL-1: "ended"/"too_long" は lifecycle 理由 = 取消対象ではない。
                # timeMin=now-15min は「終了 15 分以内」の会議も返すため、ここで bot を
                # 削除+done 化すると **全会議の transcript が回収前に破棄される** (回収は loop②)。
                if reason in ("ended", "too_long"):
                    continue
                # 予約済みだったのに不参加化 (cancelled/denylist 追記等) → bot 削除
                if entry and entry.get("bot_id") and not entry.get("done"):
                    logger.info(f"取消: {ev.get('summary', '')[:40]} ({reason})")
                    if not dry_run:
                        try:
                            delete_bot(http, entry["bot_id"])
                        except Exception as _de:
                            logger.warning(f"bot 削除失敗 (継続): {_de}")
                        entry["cancelled"] = True
                        entry["done"] = True
                    summary["cancelled"] += 1
                continue

            start = _parse_dt(start_raw)
            # ★reviewer N-4: 開始済み会議 (window は -15min を含む) は join_at が過去になり
            # Recall が拒否し得る → 今すぐ参加に clamp
            join_at = max(start - timedelta(minutes=2), now + timedelta(seconds=30))
            if entry and not entry.get("done"):
                if entry.get("start") != start_raw:
                    # 時刻変更 → 作り直し (★reviewer SF-1: 例外で cycle ごと落とさない)
                    logger.info(f"時刻変更: {ev.get('summary', '')[:40]}")
                    if not dry_run:
                        try:
                            delete_bot(http, entry["bot_id"])
                            b = create_bot(http, extract_meeting_url(ev), join_at,
                                           ev.get("summary") or "")
                            entry.update(bot_id=b.get("id", ""), start=start_raw)
                        except Exception as _re_err:
                            logger.warning(f"再予約失敗: {_re_err}")
                            summary["errors"] += 1
                    summary["rescheduled"] += 1
                continue
            if entry and entry.get("done"):
                # ★reviewer BL-2: done でも start が変わって再来した event (移動→horizon 復帰、
                # timeout 後の再開催) は新規予約に落とす。同一 start の done のみ確定 skip。
                if entry.get("start") == start_raw:
                    continue

            logger.info(f"予約: {ev.get('summary', '')[:50]} @ {start_raw}")
            if dry_run:
                summary["scheduled"] += 1
                continue
            try:
                b = create_bot(http, extract_meeting_url(ev), join_at,
                               ev.get("summary") or "")
                st["events"][eid] = {
                    "bot_id": b.get("id", ""), "start": start_raw,
                    "title": (ev.get("summary") or "")[:100],
                    "created": now.isoformat(timespec="seconds"),
                }
                # ★reviewer SF-1: create 直後に永続化 (途中 crash → bot_id 消失 → 二重 bot 防止)
                save_state(st)
                summary["scheduled"] += 1
            except Exception as e:
                logger.warning(f"bot 予約失敗: {type(e).__name__}: {e}")
                summary["errors"] += 1

        # ①.5 window から消えた event の orphan bot 掃除 (削除/horizon 外へ移動)。
        # 消えた event の bot を放置すると「旧時刻に空の会議へ参加」or「移動先で二重 bot」になる。
        # まだ始まっていない予約のみ削除 (開始済みは transcript 回収のため触らない)。
        # ★reviewer SF-5: listing が途中打ち切りの時は「消滅」判定不能 → skip (誤削除防止)
        for eid, entry in (list(st["events"].items()) if listing_complete else []):
            if entry.get("done") or eid in seen_ids or not entry.get("bot_id"):
                continue
            try:
                entry_start = _parse_dt(entry.get("start", ""))
            except Exception:
                entry_start = now
            if entry_start > now:
                logger.info(f"event 消滅 → bot 取消: {entry.get('title', '')[:40]}")
                if not dry_run:
                    try:
                        delete_bot(http, entry["bot_id"])
                    except Exception as _de:
                        logger.warning(f"orphan bot 削除失敗 (継続): {_de}")
                    # ★reviewer BL-2: done で塞がず entry ごと消す (同 event id で horizon に
                    # 戻ってきた時に新規予約へ自然に落ちる)
                    st["events"].pop(eid, None)
                summary["cancelled"] += 1

        # ② 終了 bot の transcript 回収 → ingest
        for eid, entry in list(st["events"].items()):
            if entry.get("done") or not entry.get("bot_id") or dry_run:
                continue
            start_raw = entry.get("start", "")
            try:
                start = _parse_dt(start_raw)
            except Exception:
                start = now
            if start > now:  # まだ始まってない
                continue
            try:
                bot = get_bot(http, entry["bot_id"])
            except Exception as e:
                logger.warning(f"bot 取得失敗 {entry['bot_id']}: {e}")
                # ★reviewer SF-3: silent 死対策 — 回収系の API 失敗も errors に計上
                # (新規予約が無い日でも loud_fail の判定材料になる)
                summary["errors"] += 1
                continue
            code = bot_status(bot)
            # ★2026-08-03: 「done だが録音ゼロ」を sub_code で真因つきに確定させる。
            #   これが無いと 22 件中 0 件でも「正常終了」に見え、原因が表に出ない。
            _sub = bot_failure_reason(bot)
            if _sub in NO_RECORDING_SUBCODES and not (bot.get("recordings") or []):
                logger.warning(f"bot 失敗 ({_sub}): {entry.get('title')}")
                entry["done"] = True
                entry["fatal"] = _sub
                summary.setdefault("no_recording", {})
                summary["no_recording"][_sub] = summary["no_recording"].get(_sub, 0) + 1
                _notify_fatal(entry, _sub)
                continue
            if code in ("fatal", "call_ended_without_recording"):
                logger.warning(f"bot 失敗 ({code}): {entry.get('title')}")
                entry["done"] = True
                entry["fatal"] = code
                _notify_fatal(entry, code)
                continue
            if code != "done":
                # 会議が2日前に始まってるのに done にならない = 異常放置を防ぐ
                if now - start > timedelta(hours=48):
                    entry["done"] = True
                    entry["fatal"] = f"timeout:{code}"
                    _notify_fatal(entry, f"timeout:{code}")
                continue
            # ★reviewer N-3: 回収 retry の上限 (LLM compile を毎 cycle 叩き続けない)
            entry["attempts"] = entry.get("attempts", 0) + 1
            if entry["attempts"] > 6:
                entry["done"] = True
                entry["fatal"] = "attempts_exceeded"
                _notify_fatal(entry, "attempts_exceeded")
                continue
            try:
                tdata = fetch_transcript(http, bot)
                text, speakers = build_transcript_text(tdata)
                if not text.strip():
                    entry["done"] = True
                    entry["fatal"] = "empty_transcript"
                    _notify_fatal(entry, "empty_transcript")
                    continue
                # ★fact-checker FLAG3: key= は BRAIN_EXTENSION_KEY 専用、VOICE_ALIGN_TOKEN は
                # token= param でのみ通る (P1c reviewer と同じ罠) → param 名を出し分け
                if os.getenv("BRAIN_EXTENSION_KEY"):
                    auth_q = f"key={os.getenv('BRAIN_EXTENSION_KEY')}"
                else:
                    auth_q = f"token={os.getenv('VOICE_ALIGN_TOKEN', '')}"
                r = http.post(
                    f"http://localhost:8000/api/meeting/ingest?{auth_q}",
                    json={
                        "transcript": text,
                        "source": "recall",
                        "title": entry.get("title") or "web会議",
                        "date": start.astimezone(JST).date().isoformat(),
                        "participants": speakers,
                    }, timeout=300)
                r.raise_for_status()
                _resp = r.json()
                # ★reviewer SF-2: HTTP 200 でも ok:False (privacy block) がある —
                # 成功扱いにせず fatal 記録 (transcript は raw に残らない = 意図どおり非保存)
                if not _resp.get("ok"):
                    entry["done"] = True
                    entry["fatal"] = f"ingest:{_resp.get('reason', 'ng')}"
                    _notify_fatal(entry, entry["fatal"])
                    continue
                wiki_file = _resp.get("wiki_file", "")
                entry["done"] = True
                entry["wiki_file"] = wiki_file
                summary["ingested"] += 1
                logger.info(f"議事録 → {wiki_file}")
                # ★DA R4: 取込完了後は Recall 側の録音/transcript を削除
                # (第三者クラウドに社内会議音声を残さない)
                if delete_bot_media(http, entry["bot_id"]):
                    entry["media_deleted"] = True
                try:
                    from clone_improve_lib import line_push, line_push_digest
                    line_push(f"📝 議事録できた: {entry.get('title')}\n→ {wiki_file}")
                except Exception:
                    pass
            except Exception as e:
                logger.warning(f"transcript 回収失敗 {entry['bot_id']}: {e}")
                summary["errors"] += 1

    # 30日より古い done エントリは掃除
    cutoff = (now - timedelta(days=30)).isoformat()
    st["events"] = {k: v for k, v in st["events"].items()
                    if not (v.get("done") and v.get("created", "9999") < cutoff)}
    if not dry_run:
        save_state(st)
    return summary


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true", help="予約/削除/ingest なし")
    ap.add_argument("--horizon-hours", type=int, default=36)
    args = ap.parse_args()

    if os.getenv("MEETING_AUTOJOIN_ENABLED", "0") != "1" and not args.dry_run:
        print("MEETING_AUTOJOIN_ENABLED != 1 → skip")
        return 0
    if not os.getenv("RECALL_API_KEY"):
        print("RECALL_API_KEY 未設定 → skip")
        return 0

    ok = True
    try:
        summary = run_cycle(dry_run=args.dry_run, horizon_hours=args.horizon_hours)
        print(json.dumps(summary, ensure_ascii=False, indent=2))
        ok = summary.get("errors", 0) == 0
    except Exception as e:
        logger.error(f"cycle failed: {type(e).__name__}: {e}")
        ok = False
    # §1.18: calendar/recall API の連続失敗は loud (silent 死で「議事録が来ない」に気づかない)
    if not args.dry_run:
        try:
            from clone_improve_lib import loud_fail
            loud_fail("meeting_autojoin", ok,
                      "web会議 自動参加 cycle が失敗 (calendar/Recall API)。"
                      "bot 予約と議事録回収が止まっている可能性",
                      threshold=3, cooldown_h=12)
        except Exception:
            pass
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
