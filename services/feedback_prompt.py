"""社員クローン応答への 👍👎 sampled フィードバック (★2026-07-10 世界基準評価 S3)。

利用者起点フィードバックが全期間3件・最終 2026-05-12 = 品質ループが LLM 自己評価のみで
回っていた (Product 高gap)。1:1 DM 応答に低確率で 👍👎 を出し、negative rating を品質
トリアージの入口にする。ロジックを main.py に置かず本 module に隔離 (CLAUDE.md §1.12b)。

安全設計:
- **default OFF** (`FEEDBACK_PROMPT_RATE` 未設定=0)。海山が button-tap を1度検証してから有効化
  (E2E の button 往復は当方検証不可な社員可視 UX のため)。有効化は env 1つ (rate=0.15 等)。
- 実質的な応答のみ (短い ack / エラー系は除外)。決定論 sampling (user_id+応答冒頭の hash) で
  「同じ応答には毎回出す/出さない」= 揺れない。
- button_template + postback (clonefb:good/bad) は 1:1 DM のみ (group は button 非対応)。
"""
from __future__ import annotations

import hashlib
import logging
import os

logger = logging.getLogger(__name__)

# 除外する非実質応答のマーカー (エラー/縮退/短い ack)
_SKIP_MARKERS = ("エラー", "⚠️", "一時的に", "応答できない", "管理者専用", "受けられません")
_MIN_LEN = 40   # これ未満の短い応答には出さない


def _rate() -> float:
    try:
        r = float(os.getenv("FEEDBACK_PROMPT_RATE", "0") or 0)
    except Exception:
        return 0.0
    return max(0.0, min(1.0, r))


# ★2026-07-12 海山「会話の全てに聞くのはやり過ぎ。スムーズな会話を阻害する」:
#   per-user cooldown (既定 168h = 1 人あたり最大 週 1 回)。sampling rate とは独立の上限。
_COOLDOWN_STATE = None  # 遅延 import 用 (clone_feedback の FEEDBACK_DIR に置く)


def _cooldown_h() -> float:
    try:
        return float(os.getenv("FEEDBACK_PROMPT_COOLDOWN_H", "168") or 168)
    except Exception:
        return 168.0


def _cooldown_path():
    import clone_feedback
    return clone_feedback.FEEDBACK_DIR / ".rating_prompt_last.json"


def _in_cooldown(user_id: str) -> bool:
    import json
    import time
    try:
        d = json.loads(_cooldown_path().read_text(encoding="utf-8"))
    except Exception:
        return False
    last = d.get(user_id, 0)
    return (time.time() - last) < _cooldown_h() * 3600


def _mark_prompted(user_id: str) -> None:
    import json
    import time
    p = _cooldown_path()
    try:
        d = json.loads(p.read_text(encoding="utf-8"))
    except Exception:
        d = {}
    d[user_id] = time.time()
    try:
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(json.dumps(d), encoding="utf-8")
    except Exception as e:
        logger.warning(f"rating cooldown state write failed: {e}")


def _should_prompt(user_id: str, reply: str) -> bool:
    rate = _rate()
    if rate <= 0:
        return False
    if not reply or len(reply) < _MIN_LEN:
        return False
    if any(m in reply for m in _SKIP_MARKERS):
        return False
    if _in_cooldown(user_id):
        return False
    # 決定論 sampling: 同じ (user, 応答) は毎回同じ判定 (Math.random 不使用 = 揺れない)
    h = int(hashlib.sha256((user_id + "|" + reply[:80]).encode("utf-8")).hexdigest(), 16) % 1000
    return h < int(rate * 1000)


async def maybe_prompt(http, user_id: str, trigger: str, reply: str,
                       user_display=None) -> bool:
    """応答送信後に (sampled) 👍👎 button を出す。出したら True。失敗/非対象は False。"""
    if not _should_prompt(user_id, reply):
        return False
    try:
        import clone_feedback
        import lineworks_bot
        clone_feedback.start_rating(user_id, trigger, reply, user_display=user_display)
        await lineworks_bot.send_button_template(
            http, user_id, "この回答、役に立った?",
            [{"label": "👍 役立った", "data": "clonefb:good"},
             {"label": "👎 いまいち", "data": "clonefb:bad"}],
        )
        _mark_prompted(user_id)  # ★2026-07-12 cooldown 起点 (送れた時のみ記録)
        return True
    except Exception as e:
        logger.warning(f"feedback prompt failed: {e}")
        return False


# ★2026-07-11 採用レビュー #3: 👎 は自由記述 (空約束・高摩擦) をやめ 1タップ理由ボタンに。
#   postback は ASCII (clonefb:why:num/nodata/offtopic/style)、label は日本語 20 chars 内。
_REASON_BUTTONS = [
    {"label": "数字が違う", "data": "clonefb:why:num"},
    {"label": "情報がない", "data": "clonefb:why:nodata"},
    {"label": "質問に答えてない", "data": "clonefb:why:offtopic"},
    {"label": "言い方・文体", "data": "clonefb:why:style"},
]


async def handle_rating_postback(http, user_id: str, pb_data: str,
                                 user_display=None) -> None:
    """clonefb:good / clonefb:bad / clonefb:why:<reason> postback を処理。

    - good → 即保存 + お礼。
    - bad  → 即保存 (負の signal を失わない) → 1タップ理由ボタンを follow-up。
    - why:<reason> → 直前の bad record に理由を後追い付与 + お礼。
    """
    body = pb_data[len("clonefb:"):].strip() if pb_data.startswith("clonefb:") else ""
    try:
        import clone_feedback
        import lineworks_bot

        if body.startswith("why:"):
            reason = body[len("why:"):].strip()
            ok = clone_feedback.attach_reason(user_id, reason)
            msg = ("教えてくれてありがとう、改善の材料にする。"
                   if ok else "ありがとう、受け取ったよ。")
            await lineworks_bot.send_text(http, user_id, msg)
            return

        rating = body  # "good" or "bad"
        rec = clone_feedback.save_rating(user_id, rating, user_display=user_display)
        if rec and rating == "good":
            await lineworks_bot.send_text(
                http, user_id, "フィードバックありがとう!改善に使わせてもらうね。")
        elif rec and rating == "bad":
            # 負の signal は保存済み。理由を 1タップで (選ばなくても bad は記録済み)
            await lineworks_bot.send_button_template(
                http, user_id, "ありがとう。どこがいまいちだった? (任意)", _REASON_BUTTONS)
        else:
            await lineworks_bot.send_text(
                http, user_id, "うまく記録できなかった (もう一度時間を置いて試してね)。")
    except Exception as e:
        logger.warning(f"clonefb rating handling failed: {e}")
