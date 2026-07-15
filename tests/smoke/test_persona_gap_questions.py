"""smoke: scripts/persona_gap_questions.py — 薄い次元 → 質問生成 → push。

alignment_interview(重い)は遅延 import。本 test は coverage を注入し LLM/push を mock(CI 軽量)。
"""
import asyncio
import pathlib
import sys

ROOT = pathlib.Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "scripts"))

import persona_gap_questions as pgq  # noqa: E402

THIN = [
    {"id": "values", "label": "価値観・判断軸", "depth_score": 1},
    {"id": "people", "label": "人との関わり方", "depth_score": 2},
    {"id": "risk", "label": "リスクの取り方", "depth_score": 2},
]


async def _fake_llm(prompt, **kw):
    assert "価値観・判断軸" in prompt          # 薄い次元が prompt に渡る
    assert kw.get("model") == "smart"
    return "1. 最近の意思決定で何を基準にした?\n2. 人に任せる時の線引きは?\n3. どこまでリスクを取る?"


def test_generate_questions_uses_thin_dims():
    out = asyncio.run(pgq.generate_questions(THIN, llm=_fake_llm))
    assert "意思決定" in out and out.startswith("1.")


def test_build_push_wraps():
    p = pgq.build_push("1. なんちゃら")
    assert "人格の問い" in p and "1. なんちゃら" in p and "返信" in p


def test_cadence_biweekly_runs_even_weeks_only():
    from datetime import datetime, timezone, timedelta
    jst = timezone(timedelta(hours=9))
    # ISO week 偶数 → 走る(skip False)、奇数 → skip True
    even = datetime(2026, 1, 5, 8, 0, tzinfo=jst)   # 2026-W02(偶数)
    odd = datetime(2026, 1, 12, 8, 0, tzinfo=jst)   # 2026-W03(奇数)
    assert even.isocalendar()[1] % 2 == 0 and odd.isocalendar()[1] % 2 == 1
    assert pgq._skip_for_cadence("biweekly", now=even) is False
    assert pgq._skip_for_cadence("biweekly", now=odd) is True


def test_cadence_monthly_runs_first_week_only():
    from datetime import datetime, timezone, timedelta
    jst = timezone(timedelta(hours=9))
    assert pgq._skip_for_cadence("monthly", now=datetime(2026, 6, 1, 8, 0, tzinfo=jst)) is False   # 第1月曜
    assert pgq._skip_for_cadence("monthly", now=datetime(2026, 6, 22, 8, 0, tzinfo=jst)) is True   # 月末側
    assert pgq._skip_for_cadence("weekly", now=datetime(2026, 6, 22, 8, 0, tzinfo=jst)) is False   # weekly は毎回


def test_run_dry_run_no_push():
    sent = []
    r = asyncio.run(pgq.run(dry_run=True, llm=_fake_llm,
                            get_thin=lambda n: THIN, push_fn=lambda m: sent.append(m)))
    assert r["ok"] and r["dry_run"] and r["dims"][0] == "価値観・判断軸"
    assert sent == []                          # dry-run は push しない


def test_run_pushes():
    sent = {}
    def fake_push(msg):
        sent["msg"] = msg
        return True
    r = asyncio.run(pgq.run(llm=_fake_llm, get_thin=lambda n: THIN, push_fn=fake_push))
    assert r["ok"] and r["pushed"] and "人格の問い" in sent["msg"]


def test_run_coverage_failure_is_soft():
    def boom(n):
        raise RuntimeError("no coverage")
    r = asyncio.run(pgq.run(get_thin=boom))
    assert r["ok"] is False and "coverage" in r["reason"]
