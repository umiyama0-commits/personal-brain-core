#!/usr/bin/env python3
"""scripts/persona_gap_questions.py — 人格の薄い次元を突く質問を週次 push(★2026-06-29 海山指示)。

alignment_interview の coverage(次元別の厚み)から「今いちばん薄い次元」を算出し、そこを埋める
具体的な質問を LLM で生成 → 海山へ LINE push。海山は電話(音声 alignment=既存の自動取込・同じ
薄い次元を突く)か返信で答える → 人格が薄い所から優先的に深まる。「狙い撃ち質問を定期的に」の実装。

捏造はしない(質問を出すだけ、wiki への反映は既存のレビュー/音声蒸留ゲートを通る)。
実行: python3 scripts/persona_gap_questions.py [--dry-run] (host cron 週次、cron-install LaunchAgent が登録)
"""
from __future__ import annotations

import argparse
import asyncio
import logging
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

JST = timezone(timedelta(hours=9))

sys.path.insert(0, str(Path(__file__).resolve().parent))         # scripts/ sibling
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))     # repo root (alignment_interview)

from clone_improve_lib import call_llm, line_push, line_push_digest  # noqa: E402  CI-safe

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger("persona_gap_questions")

N_DIMS = 3

GAP_PROMPT = """海山丈司の人格 wiki で、今いちばん薄い(掘れていない)次元はこれ:
{dims}

各次元について、海山が**車内の雑談で自然に答えられる具体的な問い**を 1 個ずつ、計 {n} 問つくってください。
# 制約
- 抽象的にしない。具体的に。例:「あなたの価値観は?」× → 「最近◯◯で迷った時、何を基準に決めた?」○
- 海山の関心(経営/現場/数字/人/意思決定)に寄せ、口調も砕けて。
- 番号付きで簡潔に(各 1〜2 行)。前置き・後語は不要、問いだけ。"""


def _get_thin_dims(n: int = N_DIMS) -> list[dict]:
    """alignment_interview の coverage から薄い次元 n 個。遅延 import(CI/test を軽く保つ)。"""
    import alignment_interview as ai
    return ai.coverage_report()[:n]


async def generate_questions(thin: list[dict], llm=None) -> str:
    llm = llm or call_llm
    # ★2026-07-04: 表示も選定と同じ実効 depth (45日 decay 込み)。decay で再浮上した次元を
    # 「depth 5」と表示すると「薄い次元」の枠組みと矛盾し、質問生成 LLM も誤誘導される。
    dims = "\n".join(
        f"- {d.get('label', d.get('id'))}(depth {d.get('effective_depth', d.get('depth_score', 0))})"
        for d in thin
    )
    prompt = GAP_PROMPT.format(dims=dims, n=len(thin))
    out = await llm(prompt, model="smart", max_tokens=700, temperature=0.4)
    return (out or "").strip()


def build_push(questions: str) -> str:
    return ("🧠 今回の人格の問い(薄い所から埋める)\n\n" + questions +
            "\n\n— 次の電話(音声雑談)でこの辺を話すと自動で人格に蓄積されます。ここに返信でもOK。")


def _skip_for_cadence(cadence: str, now: datetime | None = None) -> bool:
    """cron は毎週月曜起動。script 側で頻度を間引く(海山指示で 週次→隔週)。
    biweekly: 偶数 ISO 週のみ走る(隔週・決定論的)。monthly: 第1月曜のみ(day<=7)。weekly/None: 毎回。"""
    now = now or datetime.now(JST)
    if cadence == "biweekly":
        return (now.isocalendar()[1] % 2) != 0      # 奇数週 → skip
    if cadence == "monthly":
        return now.day > 7                           # cron が月曜限定 → day<=7 のみ = 第1月曜
    return False


async def run(*, dry_run: bool = False, llm=None, push_fn=None, get_thin=None) -> dict:
    push_fn = push_fn or (lambda t: line_push_digest(t, "人格質問"))  # ★2026-07-20 通知削減
    get_thin = get_thin or _get_thin_dims
    try:
        thin = get_thin(N_DIMS)
    except Exception as e:
        logger.error(f"coverage 取得失敗: {e}")
        return {"ok": False, "reason": f"coverage 取得不可: {e}"}
    if not thin:
        return {"ok": False, "reason": "薄い次元が取得できない"}
    try:
        questions = await generate_questions(thin, llm=llm)
    except Exception as e:                       # LITELLM 一時断でも cron を落とさない
        logger.error(f"質問生成失敗: {e}")
        return {"ok": False, "reason": f"質問生成失敗: {e}"}
    if not questions or len(questions) < 10:
        return {"ok": False, "reason": "質問生成失敗(空)"}
    labels = [d.get("label", d.get("id")) for d in thin]
    if dry_run:
        print(build_push(questions))
        return {"ok": True, "dry_run": True, "dims": labels}
    pushed = False
    try:
        pushed = bool(push_fn(build_push(questions)))
    except Exception as e:
        logger.warning(f"push 失敗: {e}")
    return {"ok": True, "pushed": pushed, "dims": labels}


def main() -> int:
    ap = argparse.ArgumentParser(description="人格の薄い次元を突く質問を定期 push(host cron)")
    ap.add_argument("--dry-run", action="store_true", help="生成だけ表示(push しない)")
    ap.add_argument("--cadence", choices=["weekly", "biweekly", "monthly"], default=None,
                    help="頻度の間引き(cron は毎週月曜起動、biweekly=偶数週のみ等)。手動実行では無指定で毎回走る")
    a = ap.parse_args()
    if a.cadence and not a.dry_run and _skip_for_cadence(a.cadence):
        print(f"skip ({a.cadence} cadence: 対象外の週)")
        return 0
    r = asyncio.run(run(dry_run=a.dry_run))
    print(r)
    return 0 if r.get("ok") else 1


if __name__ == "__main__":
    raise SystemExit(main())
