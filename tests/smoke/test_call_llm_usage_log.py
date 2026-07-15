"""smoke: call_llm の usage 計測 instrument(背景ジョブのコスト可視化、2026-06-30)。"""
import sys
import pathlib

ROOT = pathlib.Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "scripts"))

import clone_improve_lib as cil  # noqa: E402


def test_log_llm_usage_records(monkeypatch):
    calls = []
    import bot_events
    monkeypatch.setattr(bot_events, "log_bot_event",
                        lambda c, e, **k: calls.append((c, e, k)))
    cil._log_llm_usage("hallucination", "claude-opus-4-8",
                       {"prompt_tokens": 100, "completion_tokens": 20})
    assert len(calls) == 1
    c, e, k = calls[0]
    assert c == "hallucination" and e == "turn_finished"
    assert k["model"] == "claude-opus-4-8" and k["usage"]["prompt_tokens"] == 100


def test_log_llm_usage_failsafe():
    # usage 空/None でも例外を出さない(本処理を止めない)
    cil._log_llm_usage("x", "m", None)
    cil._log_llm_usage("x", "m", {})


def test_call_llm_has_component_param():
    import inspect
    sig = inspect.signature(cil.call_llm)
    assert "component" in sig.parameters
    assert sig.parameters["component"].default == "background"
