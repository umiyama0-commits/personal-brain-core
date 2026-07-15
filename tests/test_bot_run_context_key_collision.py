"""bot_run_context の同名キー衝突 regression (★2026-06-12 本番障害の再発防止)。

COST_TRACKING_ENABLED=1 で ctx["model"] = 実モデル名 を上書きすると、
旧実装 `log_bot_event(..., **start_fields, **ctx)` が
TypeError: got multiple values for keyword 'model' で爆死し、
全 smart 応答が「お休み」fallback になった (6/11 16:02〜6/12 修正まで)。
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))


def _events(tmp_path, monkeypatch):
    monkeypatch.setenv("BRAIN_APP_ROOT", str(tmp_path))
    import importlib
    import bot_events
    importlib.reload(bot_events)  # APP_ROOT を tmp に向け直す
    return bot_events


def _read_log(tmp_path) -> list[dict]:
    p = tmp_path / "data" / "brain" / "bot_events" / "events.jsonl"
    if not p.exists():
        return []
    return [json.loads(line) for line in p.read_text(encoding="utf-8").splitlines()]


def test_ctx_model_override_does_not_raise(tmp_path, monkeypatch):
    """★本番障害の再現形: start_fields に model、ctx にも model → ctx 優先で正常記録。"""
    be = _events(tmp_path, monkeypatch)
    with be.bot_run_context("clone_respond", user_id="u1", model="smart") as ctx:
        ctx["usage"] = {"prompt_tokens": 10}
        ctx["model"] = "claude-opus-4-8"  # COST_TRACKING の実モデル上書き
        ctx["status"] = "ok"
    rows = _read_log(tmp_path)
    fin = [r for r in rows if r.get("event") == "turn_finished"]
    assert len(fin) == 1
    assert fin[0]["model"] == "claude-opus-4-8"  # ctx が start_fields に勝つ
    assert fin[0]["usage"] == {"prompt_tokens": 10}


def test_ctx_model_override_in_failure_path(tmp_path, monkeypatch):
    """例外経路でも同名キーで爆死しない (turn_failed が記録され元例外が再raise)。"""
    import pytest
    be = _events(tmp_path, monkeypatch)
    with pytest.raises(ValueError, match="boom"):
        with be.bot_run_context("clone_respond", model="smart") as ctx:
            ctx["model"] = "claude-opus-4-8"
            raise ValueError("boom")
    rows = _read_log(tmp_path)
    failed = [r for r in rows if r.get("event") == "turn_failed"]
    assert len(failed) == 1
    assert failed[0]["model"] == "claude-opus-4-8"
    assert failed[0]["error_class"] == "ValueError"
