"""
chat_import.py — チャットエクスポートのパーサー & 取り込みパイプライン

対応フォーマット:
  - LINE iOS/Android テキストエクスポート (.txt)
  - WhatsApp iOS/Android テキストエクスポート (.txt)
  - 日本語・英語の日付ヘッダーに対応
"""

import re
import asyncio
import logging
from pathlib import Path
from collections import defaultdict
from typing import Optional

logger = logging.getLogger(__name__)

# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# LINE フォーマット
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# LINE export format: "HH:MM\tSender\tMessage"
LINE_MSG_PATTERN = re.compile(r"^(\d{1,2}:\d{2})\t(.+?)\t(.+)$")
# Date header: "2026/04/14(月)" or "2026.04.17 金曜日" or "Monday, April 14, 2026" etc.
# ★2026-07-05: ドット区切り "2026.04.17 金曜日" variant (海山の実 export で確認) を追加
LINE_DATE_PATTERN = re.compile(r"^(\d{4}[/.\-]\d{1,2}[/.\-]\d{1,2})")
LINE_DATE_EN_PATTERN = re.compile(
    r"^(?:Monday|Tuesday|Wednesday|Thursday|Friday|Saturday|Sunday),\s+(\w+ \d{1,2}, \d{4})"
)
# ★2026-07-05 空白区切り variant: "HH:MM Sender Message" (タブ無し、sender に空白を含み得る)。
# 海山の 2026 実 export はこの形式 + 複数行メッセージ (継続行は時刻 prefix 無し)。
LINE_MSG_SPACE_PATTERN = re.compile(r"^(\d{1,2}:\d{2}) (\S.*)$")

# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# WhatsApp フォーマット
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# iOS: "[DD/MM/YYYY, HH:MM:SS] Sender: Message"  (ブラケット付き)
#   U+200E (LTR mark) が先頭や送信者名前に付くことがある
# Android: "DD/MM/YYYY, HH:MM - Sender: Message"  (ダッシュ区切り)
WA_IOS_PATTERN = re.compile(
    r"^\u200e?\[(\d{1,2}/\d{1,2}/\d{2,4}),\s*(\d{1,2}:\d{2}(?::\d{2})?(?:\s?[AP]M)?)\]\s*\u200e?([^:]+?):\s*(.*)$"
)
# \u26052026-07-05 iOS \u65e5\u672c\u8a9e\u30ed\u30b1\u30fc\u30eb variant: "[24/7/25 \u5348\u5f8c5:46:23] Sender: Message"
# (\u30ab\u30f3\u30de\u7121\u3057\u3001\u5348\u524d/\u5348\u5f8c \u304c\u6642\u523b\u306e\u524d\u3002\u6d77\u5c71\u306e Lenskart \u7cfb\u5b9f export \u3067\u78ba\u8a8d)
WA_IOS_JP_PATTERN = re.compile(
    r"^\u200e?\[(\d{1,2}/\d{1,2}/\d{2,4}),?\s*(\u5348\u524d|\u5348\u5f8c)(\d{1,2}:\d{2}(?::\d{2})?)\]\s*\u200e?([^:]+?):\s*(.*)$"
)
WA_ANDROID_PATTERN = re.compile(
    r"^(\d{1,2}/\d{1,2}/\d{2,4}),\s*(\d{1,2}:\d{2}(?::\d{2})?(?:\s?[AP]M)?)\s*-\s*([^:]+?):\s*(.*)$"
)
# WhatsApp の E2E 暗号化ヘッダー（エクスポート先頭に必ず出現する強シグナル）
WA_E2E_SIGNALS = (
    "end-to-end encrypted",
    "エンドツーエンドで暗号化",  # 日本語
    "端對端加密",                 # 繁体字
    "端到端加密",                 # 簡体字
)


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# 判定ヘルパー
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

def is_line_chat_export(text: str, sample_lines: int = 200) -> bool:
    """テキストが LINE チャットエクスポート形式かどうか判定。

    判定基準:
      強シグナル: 先頭500字に "[LINE]" + "トーク履歴" を含む
      弱シグナル: メッセージパターン (HH:MM\\tSender\\tMessage) が3行以上 + 日付ヘッダー1行以上
    """
    if not text:
        return False
    head = text[:500]
    if "[LINE]" in head and ("トーク履歴" in head or "Chat history" in head):
        return True
    msg_count = 0
    date_count = 0
    for line in text.splitlines()[:sample_lines]:
        # ★2026-07-05: 空白区切り variant も弱シグナルに含める (日付ヘッダー必須なので
        # 「時刻で始まる一般テキスト」の誤判定は date_count 条件が防ぐ)
        if LINE_MSG_PATTERN.match(line) or LINE_MSG_SPACE_PATTERN.match(line):
            msg_count += 1
        elif LINE_DATE_PATTERN.match(line) or LINE_DATE_EN_PATTERN.match(line):
            date_count += 1
        if msg_count >= 3 and date_count >= 1:
            return True
    return False


def is_whatsapp_chat_export(text: str, sample_lines: int = 200) -> bool:
    """テキストが WhatsApp エクスポート形式かどうか判定。

    判定基準:
      強シグナル: 先頭2000字に E2E 暗号化フレーズ
      弱シグナル: iOS/Android メッセージパターンが5行以上
    """
    if not text:
        return False
    head = text[:2000]
    for sig in WA_E2E_SIGNALS:
        if sig in head:
            return True
    msg_count = 0
    for line in text.splitlines()[:sample_lines]:
        if (WA_IOS_PATTERN.match(line) or WA_ANDROID_PATTERN.match(line)
                or WA_IOS_JP_PATTERN.match(line)):
            msg_count += 1
        if msg_count >= 5:
            return True
    return False


def detect_chat_format(text: str) -> Optional[str]:
    """チャットエクスポートのフォーマットを判定。"line"|"whatsapp"|None"""
    if is_line_chat_export(text):
        return "line"
    if is_whatsapp_chat_export(text):
        return "whatsapp"
    return None


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# パーサー
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

def _infer_space_variant_senders(lines: list[str]) -> list[str]:
    """空白区切り variant の sender 名集合を頻度から推定する。

    sender は空白を含み得る ("Take Umiyama" / "MCPI H.Fukuda" / "長尾貴之(Takky)") ため
    区切りが曖昧 → 2 パス方式: 全メッセージ行の先頭 1..3 token prefix の出現頻度を数え、
    「拡張 prefix が元の頻度の 8 割以上を保持する限り伸ばす」で本名を決める
    (= "Take" は常に "Take Umiyama" が続くので 2 token、"nakatani" の次語はバラけるので 1 token)。
    返り値は長い順 (parse 時に最長一致させる)。
    """
    from collections import Counter
    counts = [Counter(), Counter(), Counter()]
    for line in lines:
        m = LINE_MSG_SPACE_PATTERN.match(line)
        if not m:
            continue
        toks = m.group(2).split(" ")
        for k in (1, 2, 3):
            if len(toks) >= k:
                counts[k - 1][" ".join(toks[:k])] += 1

    senders: set[str] = set()
    for cand, n in counts[0].items():
        if n < 3:   # 低頻度 prefix は sender と断定しない (parse 時は先頭 1 token に fallback)
            continue
        best, cur_n = cand, n
        for k in (2, 3):
            exts = [(c, m2) for c, m2 in counts[k - 1].items()
                    if c.startswith(best + " ") and m2 >= 0.8 * cur_n]
            if not exts:
                break
            best, cur_n = max(exts, key=lambda x: x[1])
        senders.add(best)
    return sorted(senders, key=len, reverse=True)


def parse_line_export(file_path: Path) -> list[dict]:
    """LINE チャットエクスポート .txt をパースして構造化メッセージリストを返す。

    2 variant 対応 (★2026-07-05 空白区切りを追加):
      - タブ区切り:  "HH:MM\\tSender\\tMessage" (従来)
      - 空白区切り:  "HH:MM Sender Message" + 複数行メッセージ (継続行は時刻 prefix 無し)
    日付ヘッダーは "2026/04/14" / "2026-04-14" / "2026.04.17 金曜日" / 英語形式。
    日付は YYYY-MM-DD に正規化して返す。
    """
    messages: list[dict] = []
    current_date = ""
    text = file_path.read_text(encoding="utf-8")
    lines = text.split("\n")

    # variant 判定: 先頭 400 行でタブ形式が成立しなければ空白形式とみなす
    tab_hits = sum(1 for l in lines[:400] if LINE_MSG_PATTERN.match(l))
    space_hits = sum(1 for l in lines[:400] if LINE_MSG_SPACE_PATTERN.match(l))
    use_space = space_hits >= 3 and tab_hits < 3
    senders = _infer_space_variant_senders(lines) if use_space else []

    current: Optional[dict] = None

    def _flush():
        nonlocal current
        if current is not None:
            messages.append(current)
            current = None

    for line in lines:
        line = line.rstrip("\r").rstrip()
        if not line:
            continue

        # 日付ヘッダー (正規化: 2026.04.17 / 2026/04/17 → 2026-04-17)
        date_match = LINE_DATE_PATTERN.match(line)
        if date_match:
            _flush()
            current_date = date_match.group(1).replace(".", "-").replace("/", "-")
            continue
        date_en_match = LINE_DATE_EN_PATTERN.match(line)
        if date_en_match:
            _flush()
            current_date = date_en_match.group(1)
            continue

        # タブ区切りメッセージ行 (従来形式)
        msg_match = LINE_MSG_PATTERN.match(line)
        if msg_match:
            _flush()
            time_str, sender, content = msg_match.groups()
            messages.append({
                "date": current_date,
                "time": time_str,
                "sender": sender,
                "text": content,
            })
            continue

        if use_space:
            m = LINE_MSG_SPACE_PATTERN.match(line)
            if m:
                _flush()
                time_str, rest = m.groups()
                sender, content = "", rest
                for s in senders:                     # 最長一致
                    if rest == s or rest.startswith(s + " "):
                        sender = s
                        content = rest[len(s):].lstrip(" ")
                        break
                if not sender:                        # 未知 sender は先頭 1 token に fallback
                    parts = rest.split(" ", 1)
                    sender = parts[0]
                    content = parts[1] if len(parts) > 1 else ""
                current = {
                    "date": current_date,
                    "time": time_str,
                    "sender": sender,
                    "text": content,
                }
                continue
            # 継続行 (複数行メッセージ) — 従来 parser は捨てていた内容を保全
            if current is not None:
                current["text"] += "\n" + line

    _flush()
    return messages


def _normalize_wa_date(date_str: str) -> str:
    """WhatsApp の日付を YYYY-MM-DD に正規化。

    DD/MM/YYYY をデフォルト（グローバル標準）。
    最初の値が 12 以下かつ 2番目が 13 以上なら MM/DD/YYYY と判断。
    """
    parts = date_str.split("/")
    if len(parts) != 3:
        return date_str
    a, b, y = parts
    y = y if len(y) == 4 else f"20{y}"
    try:
        ai, bi = int(a), int(b)
        # MM/DD/YYYY パターン: 2番目が 13 以上なら day（米国形式確定）
        if ai <= 12 and bi > 12:
            mm, dd = ai, bi
        else:
            # デフォルト DD/MM/YYYY
            dd, mm = ai, bi
        return f"{y}-{mm:02d}-{dd:02d}"
    except ValueError:
        return date_str


def parse_whatsapp_export(file_path: Path) -> list[dict]:
    """WhatsApp チャットエクスポート .txt をパース。

    - iOS: "[DD/MM/YYYY, HH:MM:SS] Sender: Message"
    - Android: "DD/MM/YYYY, HH:MM - Sender: Message"
    - 複数行メッセージ（次行が日付で始まらない場合）は前メッセージに連結
    - システムメッセージ（"end-to-end encrypted" 等、コロン無し）はスキップ
    """
    messages = []
    text = file_path.read_text(encoding="utf-8")
    current = None

    for line in text.split("\n"):
        line = line.rstrip()
        if not line:
            continue

        m = WA_IOS_PATTERN.match(line) or WA_ANDROID_PATTERN.match(line)
        jp = None if m else WA_IOS_JP_PATTERN.match(line)
        if m:
            # 前のメッセージ確定
            if current:
                messages.append(current)
            date_str, time_str, sender, content = m.groups()
            current = {
                "date": _normalize_wa_date(date_str),
                "time": time_str.strip(),
                "sender": sender.strip().lstrip("\u200e"),
                "text": content.strip(),
            }
        elif jp:
            # ★2026-07-05 日本語ロケール: "[24/7/25 午後5:46:23] Sender: msg" → 24h 変換
            if current:
                messages.append(current)
            date_str, ampm, time_str, sender, content = jp.groups()
            h, rest = time_str.split(":", 1)
            hi = int(h)
            if ampm == "午後" and hi < 12:
                hi += 12
            elif ampm == "午前" and hi == 12:
                hi = 0
            current = {
                "date": _normalize_wa_date(date_str),
                "time": f"{hi:02d}:{rest}",
                "sender": sender.strip().lstrip("\u200e"),
                "text": content.strip(),
            }
        elif current:
            # 複数行メッセージの継続
            current["text"] += "\n" + line

    if current:
        messages.append(current)

    return messages


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# カウンターパーティ（相手名）推定
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

def _extract_counterparty(file_path: Path, fmt: str) -> str:
    """ファイル名から会話相手名を抽出。"""
    stem = file_path.stem
    # 共通：タイムスタンププレフィックスを剥がす
    # 例: "line_chat_20260422_050407_LINEKeitaObaとのトーク"
    # 例: "whatsapp_chat_20260422_050407_WhatsApp_Chat_-_John"
    # 例: "whatsapp_chat_20260422_050407_WhatsApp_Chat_with_John"

    if fmt == "line":
        if "_LINE" in stem:
            stem = stem.split("_LINE", 1)[1]
    elif fmt == "whatsapp":
        # 色々な表記揺れに対応（より特異性の高いマーカーを先に）
        for marker in [
            "WhatsApp_Chat_with_",
            "WhatsApp_Chat_-_",
            "WhatsApp Chat with ",
            "WhatsApp Chat - ",
            "WhatsAppChatwith_",
            "WhatsAppChat-_",
            "WhatsAppChatwith",
            "WhatsAppChat-",
            "_WhatsApp_Chat_",
            "_WhatsAppChat_",
            "_WhatsAppChat-",
            "_WhatsApp",
        ]:
            if marker in stem:
                stem = stem.split(marker, 1)[1]
                break

    return stem.lstrip("_ -") or "unknown"


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# 取り込みパイプライン
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

async def process_chat_export(file_path: Path, privacy, brain) -> dict:
    """
    チャットエクスポート(.txt)を PrivacyGate → BrainWiki に取り込む。

    LINE / WhatsApp を自動判定し、適切なパーサーにディスパッチ。

    バッチ戦略（コスト最適化）:
      - 日付単位でメッセージ集約 → PrivacyGate を 1 日 1 回呼び出し
      - 1 ファイル = 1 ingest_note（カウンターパーティ別に集約）
      - 数千メッセージのファイルでも LLM 呼び出しは数十回に圧縮される
    """
    text = file_path.read_text(encoding="utf-8", errors="replace")
    fmt = detect_chat_format(text)
    if fmt == "line":
        messages = parse_line_export(file_path)
    elif fmt == "whatsapp":
        messages = parse_whatsapp_export(file_path)
    else:
        logger.info(f"Unknown chat format, skipping: {file_path.name}")
        return {"total": 0, "allowed": 0, "blocked": 0, "format": "unknown"}

    if not messages:
        return {"total": 0, "allowed": 0, "blocked": 0, "format": fmt}

    counterparty = _extract_counterparty(file_path, fmt)

    # 日付ごとに集約
    by_date = defaultdict(list)
    for msg in messages:
        by_date[msg["date"]].append(msg)

    sorted_dates = sorted(by_date.keys())

    # 日付ブロック単位で並列フィルタ（concurrency=10 で 10x 高速化）
    sem = asyncio.Semaphore(10)

    async def _filter_one(date_key):
        msgs = by_date[date_key]
        block_text = "\n".join(
            f"[{m['time']}] {m['sender']}: {m['text']}" for m in msgs
        )
        async with sem:
            result = await privacy.filter(block_text, sender_id=counterparty)
        return date_key, msgs, result

    filter_results = await asyncio.gather(
        *[_filter_one(d) for d in sorted_dates], return_exceptions=True
    )

    allowed_msgs = 0
    blocked_msgs = 0
    sections = []

    for item in filter_results:
        if isinstance(item, Exception):
            logger.warning(f"Filter error: {item}")
            continue
        date_key, msgs, result = item
        if result.verdict.value == "allow":
            safe_date = str(date_key).replace("/", "-").replace("\\", "-")
            sections.append(f"## {safe_date}\n\n{result.sanitized}")
            allowed_msgs += len(msgs)
        else:
            blocked_msgs += len(msgs)

    if sections:
        # ファイル単位で 1 回だけ ingest
        source_label = "LINE chat" if fmt == "line" else "WhatsApp chat"
        combined = f"# {source_label}: {counterparty}\n\n" + "\n\n".join(sections)
        safe_name = "".join(
            c if (c.isalnum() or c in "._-" or ord(c) > 127) else "_"
            for c in counterparty
        )[:50] or "unknown"
        prefix = "line" if fmt == "line" else "whatsapp"
        await brain.ingest_note("import", combined, title=f"{prefix}_{safe_name}")

    logger.info(
        f"Imported [{fmt}] {file_path.name}: {allowed_msgs} allowed, {blocked_msgs} blocked "
        f"from {len(messages)} messages across {len(by_date)} days "
        f"(LLM calls: {len(by_date)})"
    )
    return {
        "total": len(messages),
        "allowed": allowed_msgs,
        "blocked": blocked_msgs,
        "days": len(by_date),
        "format": fmt,
    }
