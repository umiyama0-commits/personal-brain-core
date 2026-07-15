"""services/lineworks_onboarding.py — うみやまAI 初回オンボーディング (LINE Works 社員向け).

★2026-07-11 採用/定着レビュー (20 agent workflow) の #1 提案:
- 旧 welcome は initial commit のまま「資料・議事録・まとめ作業は引き受けない」を宣言し、
  2026-07-11 の業務エージェント転換 (M4 = 簡易作業の代行解禁) と正反対だった。
  → 唯一のオンボーディング面が転換を否定 = 転換が定着しない構造要因。
- 累計 114 人中 44% が「利用開始」ボタンだけ押して一問も聞かず離脱 (first-run 転換の欠落)。
  → タップ即質問できる例文ボタンを welcome に添え、初回の「何を聞けばいいか分からない」を除去。

例文ボタンは **本番 green 実証済み** のクエリのみ (売上・戦略 = bot のコア。制度系は Drive 403 で
未取込のため今は載せない = #2 修理後に追加)。ボタン tap は label がそのまま text メッセージとして
届き通常フローで処理される (postback prefix 無し = drv:/clonefb: と衝突しない)。§1.12b: main.py は
wiring のみ、文言/ロジックはここ。
"""
from __future__ import annotations

import logging

import httpx

logger = logging.getLogger(__name__)

WELCOME_TEXT = (
    "はじめまして、うみやまAI です。\n"
    "海山の AI 分身。「海山ならどう考えるか / どう判断するか」を返す相談相手で、\n"
    "簡単な作業なら代わりにやる。まずは気軽に聞いてみて。\n"
    "\n"
    "【できること】\n"
    "・業務データの即答 — 店舗売上・予算・客数・ランキング、商圏や出店候補も\n"
    "・相談の壁打ち / 迷った時の「海山ならどう見るか」の視点出し\n"
    "・OWNDAYS の戦略・文化・バリューの確認\n"
    "・簡単な作業の代行 — 要約・比較表・短い文案・議事録やメモの整理\n"
    "  (資料や議事録は本文を貼るか、ファイルを添付してくれれば整理する)\n"
    "\n"
    "【大きめの作業は】\n"
    "・プレゼン資料や込み入った企画は Gemini / Claude / ChatGPT が向いてる。\n"
    "  頼んでくれれば、そのまま使える依頼プロンプトはこちらで作る。\n"
    "・個別の人事評価・処遇の文書は代筆しない。未公開案件は海山本人に。\n"
    "\n"
    "【返事が違ってたら、そのまま返信で教えて】\n"
    "・「違う」「正しくは…」で書き始めると修正として記録し、海山がレビューして反映する\n"
    "\n"
    "・私は AI で、海山本人ではない / やりとりは海山も見られる (一緒に学ぶため)\n"
    "・医療・法律・深刻なメンタルは専門家にも / 大事な判断は最後は自分で\n"
    "\n"
    "下のボタンから試せるよ。気軽にどうぞ。"
)

# 例文ボタン: label がそのまま送信クエリになる (≤20 chars 制約、postback 無し = 通常フロー処理)。
# ★本番 green のクエリのみ (売上=owndays-daily-sales/monday-dash core、戦略=canon 常駐)。
# 制度・商圏系は #2 (規程 Drive 403 修理) / URL 露出の海山判断が済んでから追加する。
EXAMPLE_QUERIES = (
    "今月の全社売上は?",
    "先週の売上は?",
    "OWNDAYSの戦略は?",
)

_BUTTON_PROMPT = "まずはこのあたりから 👇 (タップで質問できる)"


async def send_welcome(http: httpx.AsyncClient, user_id: str) -> None:
    """初回接触時に welcome 本文 + 例文ボタンを送る。ボタン送信失敗は本文送信を妨げない
    (send_button_template 自体が text fallback を内蔵)。"""
    import lineworks_bot
    await lineworks_bot.send_text(http, user_id, WELCOME_TEXT)
    try:
        await lineworks_bot.send_button_template(
            http, user_id, _BUTTON_PROMPT,
            [{"label": q} for q in EXAMPLE_QUERIES],
        )
    except Exception as e:  # fallback も失敗 = 本文は届いているので warn 止まり
        logger.warning(f"welcome 例文ボタン送信失敗 (本文は送信済): {e}")
