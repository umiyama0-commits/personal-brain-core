"""privacy_gate Gate2 の fail-safe test。

★2026-06-08 システム評価 Security HIGH: LLM 分類失敗時に素通り (fail-open) すると未分類の
personal データが公開 clone に載る。失敗時は QUARANTINE に倒す (fail-safe) ことを固定する。
正常経路 (短文/無効/whitelist の通過) は変えないことも確認。
"""
import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from privacy_gate import gate2_llm_classify, Verdict  # noqa: E402

_CFG = {"llm_classify": {"enabled": True, "model": "fast"}, "whitelisted_keywords": []}
_LONG = "これは十分に長い社員の個人的な相談メッセージで分類が要る内容です"


class _BoomClient:
    async def post(self, *a, **k):
        raise RuntimeError("LLM down")


class _Resp:
    def __init__(self, payload):
        self._p = payload

    def raise_for_status(self):
        return None

    def json(self):
        return self._p


class _OkClient:
    """classification を固定で返す mock。"""
    def __init__(self, classification):
        self._c = classification

    async def post(self, *a, **k):
        import json as _j
        content = _j.dumps({"classification": self._c, "reason": "test"})
        return _Resp({"choices": [{"message": {"content": content}}]})


def _run(coro):
    return asyncio.run(coro)


def test_llm_exception_quarantines_not_passes():
    r = _run(gate2_llm_classify(_LONG, _CFG, _BoomClient(), "http://x", "k"))
    assert r is not None
    assert r.verdict == Verdict.QUARANTINE   # fail-open でなく fail-safe


def test_short_text_still_passes():
    # 短文 (<10) はそもそも分類スキップ = 通過 (挙動不変)
    r = _run(gate2_llm_classify("やあ", _CFG, _BoomClient(), "http://x", "k"))
    assert r is None


def test_disabled_still_passes():
    cfg = {"llm_classify": {"enabled": False}}
    r = _run(gate2_llm_classify(_LONG, cfg, _BoomClient(), "http://x", "k"))
    assert r is None


def test_exclude_still_blocks():
    r = _run(gate2_llm_classify(_LONG, _CFG, _OkClient("exclude"), "http://x", "k"))
    assert r is not None and r.verdict == Verdict.BLOCK


def test_work_still_passes():
    r = _run(gate2_llm_classify(_LONG, _CFG, _OkClient("work"), "http://x", "k"))
    assert r is None  # 正常な work 分類は通過 (挙動不変)
