"""compile_number_scrub — bot 発話由来の数値を wiki 昇格させないガード (★2026-07-13).

背景 (failure-log 2026-07-13): クローンが捏造した関東売上 (例: 客数 1,234 / 12,345,678円 のような実在しない精密値) が、
会話ログの compile で wiki/decisions/ に confidence: high の「事実」として昇格した。放置すると
次の同種質問で retrieval がこの note を引き、捏造値が再回答される自己汚染ループが閉じる。

方針: **bot の発話数値は真偽を問わず wiki の一次情報にしない**。正しい場合でも出所は knowledge/
の確定データであり、note への複製は鮮度切れ・改変リスクしか生まない (リンクで参照すべき)。
会話 raw の話者ラベル ([HH:MM] 話者名:) を決定論でパースし、compile 出力中の数値 token のうち
「人間発話に存在しないもの」を 〔数値略〕 に置換する。行ごと消さない = 定性的文脈は保持。

pure function (LLM 不使用)。呼び手は brain_wiki.py の compile updates 適用直前 1 箇所。
COMPILE_PROMPT 側の指示 (書くな) と二重防御 — prompt は破られる前提で、この scrub が実防御。
"""
from __future__ import annotations

import os
import re

# "[14:53] うみやまAI: ..." / 全角コロン対応。時刻は H:MM or HH:MM(:SS)
_SPEAKER_RE = re.compile(r"^\[\d{1,2}:\d{2}(?::\d{2})?\]\s*([^:：]{1,40})[:：]\s?(.*)$")
# ingest_conversation (brain_wiki.py) の Markdown 話者形式 "**User**: ..." / "**AI**: ..."
# ★2026-07-13 cross-check DA (REAL 高): この形式が常時稼働の会話 compile 経路の支配的
# フォーマットで、初版は [HH:MM] 形式しか見ておらず本番経路で scrub が no-op だった。
_MD_SPEAKER_RE = re.compile(r"^\*\*([^*]{1,40})\*\*\s*[:：]\s?(.*)$")
# 数値 token: カンマ区切り数 / 億万金額 / 裸の 5 桁以上 (電話番号・ID は wiki 化対象外の想定)
_TOKEN_RE = re.compile(r"\d{1,3}(?:,\d{3})+(?:\.\d+)?|[\d.]+(?:億|万)|\d{5,}")

REDACTED = "〔数値略〕"


def _bot_names() -> tuple[str, ...]:
    env = os.getenv("COMPILE_SCRUB_BOT_NAMES", "")
    if env.strip():
        return tuple(n.strip() for n in env.split(",") if n.strip())
    return ("うみやまAI",)


def split_speaker_text(raw: str, bot_names: tuple[str, ...]) -> tuple[str, str] | None:
    """raw を (人間側テキスト, bot 側テキスト) に分離。話者行が無い/bot 行が無い場合は None
    (= 会話ログではない source。scrub 対象外)。継続行は直前の話者に帰属。
    対応形式: ①"[HH:MM] 名前:" (lineworks scrape / chat_import) ②"**名前**:"
    (ingest_conversation。**AI** ラベルは bot 固定)。"""

    def _is_bot(name: str) -> bool:
        n = name.strip()
        return n == "AI" or any(b in n for b in bot_names)

    human, bot = [], []
    current = None  # None | "human" | "bot"
    saw_bot = False
    for ln in raw.splitlines():
        m = _SPEAKER_RE.match(ln) or _MD_SPEAKER_RE.match(ln)
        if m:
            if _is_bot(m.group(1)):
                current = "bot"
                saw_bot = True
                bot.append(m.group(2))
            else:
                current = "human"
                human.append(m.group(2))
        elif current == "bot":
            bot.append(ln)
        elif current == "human":
            human.append(ln)
        else:
            # 話者出現前のヘッダ等は中立 = 人間側扱い (frontmatter の日付等を誤検知しない)
            human.append(ln)
    if not saw_bot:
        return None
    return "\n".join(human), "\n".join(bot)


def scrub_bot_numbers(compiled: str, raw: str,
                      bot_names: tuple[str, ...] | None = None) -> tuple[str, int]:
    """compile 出力 (wiki へ書く content) から、人間発話に存在しない数値 token を置換。
    返り値: (scrub 後テキスト, 置換した token 数)。会話ログでない source は無変更。"""
    if not compiled or not raw:
        return compiled, 0
    parts = split_speaker_text(raw, bot_names or _bot_names())
    if parts is None:
        return compiled, 0
    human_text, _ = parts
    allowed = set(_TOKEN_RE.findall(human_text))
    n = 0

    def _sub(m: re.Match) -> str:
        nonlocal n
        tok = m.group(0)
        if tok in allowed:
            return tok
        n += 1
        return REDACTED

    out = _TOKEN_RE.sub(_sub, compiled)
    if n:
        out += ("\n\n> ※bot 発話にのみ由来する数値 {} 件は捏造リスクのため wiki 化していない "
                "(確定値は knowledge/ の日次履歴・DB が正)。".format(n))
    return out, n
