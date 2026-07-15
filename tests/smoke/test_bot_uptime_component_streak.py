"""smoke test: bot_uptime_monitor の component-level streak (★2026-05-24 Tier 1)

Cohere Rerank / Drive / group context update 等の個別 component で
3 連続失敗を検知できるか。
"""
from __future__ import annotations

import importlib
import json
import sys
from datetime import datetime, timezone, timedelta
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(REPO))
sys.path.insert(0, str(REPO / "scripts"))


def _write_events(tmp_path, events):
    """tmp_path/data/brain/bot_events/events.jsonl に events を書き込む。"""
    log_dir = tmp_path / "data" / "brain" / "bot_events"
    log_dir.mkdir(parents=True, exist_ok=True)
    log_file = log_dir / "events.jsonl"
    with log_file.open("w", encoding="utf-8") as f:
        for ev in events:
            f.write(json.dumps(ev, ensure_ascii=False) + "\n")
    return log_file


@pytest.mark.smoke
def test_component_streak_no_failures(tmp_path, monkeypatch):
    """全 component 失敗 0 件 → has_any_burst False。"""
    monkeypatch.setenv("BRAIN_ROOT", str(tmp_path / "data" / "brain"))
    _write_events(tmp_path, [
        {"ts": datetime.now().isoformat(), "event": "turn_finished", "component": "clone_respond"},
    ])
    import bot_uptime_monitor as bum
    importlib.reload(bum)
    result = bum.check_component_streak()
    assert result["ok"] is True
    assert result["has_any_burst"] is False
    assert result["burst_components"] == []


@pytest.mark.smoke
def test_component_streak_cohere_burst(tmp_path, monkeypatch):
    """cohere_rerank が 3 件失敗 → burst で検知。"""
    monkeypatch.setenv("BRAIN_ROOT", str(tmp_path / "data" / "brain"))
    now = datetime.now()
    events = [
        {"ts": (now - timedelta(minutes=i*5)).isoformat(),
         "event": "turn_failed", "component": "cohere_rerank",
         "error_class": "HTTPError", "error_msg": "401 Unauthorized"}
        for i in range(3)
    ]
    _write_events(tmp_path, events)
    import bot_uptime_monitor as bum
    importlib.reload(bum)
    result = bum.check_component_streak()
    assert result["ok"] is True
    assert result["has_any_burst"] is True
    assert "cohere_rerank" in result["burst_components"]
    assert result["per_component"]["cohere_rerank"]["n_failed"] == 3


@pytest.mark.smoke
def test_component_streak_multiple_components(tmp_path, monkeypatch):
    """複数 component が同時 burst → 全て burst_components に含まれる。"""
    monkeypatch.setenv("BRAIN_ROOT", str(tmp_path / "data" / "brain"))
    now = datetime.now()
    events = []
    # cohere_rerank: 3 件 failed
    for i in range(3):
        events.append({"ts": (now - timedelta(minutes=i*5)).isoformat(),
                       "event": "turn_failed", "component": "cohere_rerank"})
    # drive_ingest: 4 件 failed
    for i in range(4):
        events.append({"ts": (now - timedelta(minutes=i*5)).isoformat(),
                       "event": "turn_failed", "component": "drive_ingest"})
    # clone_respond: 1 件 failed (threshold 3 未満)
    events.append({"ts": now.isoformat(),
                   "event": "turn_failed", "component": "clone_respond"})
    _write_events(tmp_path, events)
    import bot_uptime_monitor as bum
    importlib.reload(bum)
    result = bum.check_component_streak()
    assert result["has_any_burst"] is True
    bursts = set(result["burst_components"])
    assert "cohere_rerank" in bursts
    assert "drive_ingest" in bursts
    assert "clone_respond" not in bursts  # threshold 未満


@pytest.mark.smoke
def test_component_streak_below_threshold(tmp_path, monkeypatch):
    """threshold 未満 → is_burst False、ただし per_component には count 記録。"""
    monkeypatch.setenv("BRAIN_ROOT", str(tmp_path / "data" / "brain"))
    now = datetime.now()
    events = [
        {"ts": (now - timedelta(minutes=i*5)).isoformat(),
         "event": "turn_failed", "component": "clone_group_context_update"}
        for i in range(2)  # threshold 3 未満
    ]
    _write_events(tmp_path, events)
    import bot_uptime_monitor as bum
    importlib.reload(bum)
    result = bum.check_component_streak()
    assert result["has_any_burst"] is False
    info = result["per_component"]["clone_group_context_update"]
    assert info["n_failed"] == 2
    assert info["is_burst"] is False


@pytest.mark.smoke
def test_component_streak_ignores_finished_events(tmp_path, monkeypatch):
    """turn_finished は count しない (= 成功 event)、turn_failed のみ。"""
    monkeypatch.setenv("BRAIN_ROOT", str(tmp_path / "data" / "brain"))
    now = datetime.now()
    events = [
        {"ts": (now - timedelta(minutes=i*5)).isoformat(),
         "event": "turn_finished", "component": "cohere_rerank"}
        for i in range(10)
    ]
    _write_events(tmp_path, events)
    import bot_uptime_monitor as bum
    importlib.reload(bum)
    result = bum.check_component_streak()
    assert result["has_any_burst"] is False
    assert result["per_component"]["cohere_rerank"]["n_failed"] == 0


@pytest.mark.smoke
def test_critical_components_includes_new_tier0_components():
    """CRITICAL_COMPONENTS に Tier 0 / Tier 1 関連 component が含まれる。"""
    import bot_uptime_monitor as bum
    importlib.reload(bum)
    comp_names = {c for c, _l in bum.CRITICAL_COMPONENTS}
    # Tier 0 で追加した group 関連
    assert "clone_group_context_update" in comp_names
    # Plan C v2 Step 2 で追加した cohere
    assert "cohere_rerank" in comp_names
    # Google Drive 連携
    assert "drive_ingest" in comp_names
    # 既存 core
    assert "clone_respond" in comp_names
    assert "clone_memory_update" in comp_names
    assert "sleep_time" in comp_names


@pytest.mark.smoke
def test_remediation_hints_per_component():
    """各 critical component に REMEDIATION_HINTS が定義されてる。"""
    import bot_uptime_monitor as bum
    importlib.reload(bum)
    for comp, _label in bum.CRITICAL_COMPONENTS:
        hint_key = f"component_streak_{comp}"
        assert hint_key in bum.REMEDIATION_HINTS, f"hint missing for {comp}"
        assert len(bum.REMEDIATION_HINTS[hint_key]) > 10  # 中身ある


# ─── cohere_rerank / drive_ingest の failure event 発火 verify ───
# (= 既存 graceful degradation 設計を壊さず log_bot_event を追加した部分)


@pytest.mark.smoke
async def test_cohere_rerank_logs_failure_event_on_http_error(tmp_path, monkeypatch):
    """Cohere API HTTP error 時、turn_failed event が記録される (graceful degradation 維持)。"""
    monkeypatch.setenv("BRAIN_ROOT", str(tmp_path / "data" / "brain"))
    monkeypatch.setenv("COHERE_API_KEY", "test_key")

    from unittest.mock import AsyncMock, MagicMock
    import httpx as _httpx

    # http.post が HTTPStatusError raise
    http_mock = AsyncMock()
    fake_resp = MagicMock()
    fake_resp.raise_for_status = MagicMock(side_effect=_httpx.HTTPStatusError(
        "401", request=MagicMock(), response=MagicMock(status_code=401, text="Unauthorized")
    ))
    http_mock.post = AsyncMock(return_value=fake_resp)

    from brain_wiki_helpers.rerank import cohere_rerank
    result = await cohere_rerank(query="test", documents=["d1"], http=http_mock)
    # graceful degradation: None 返却
    assert result is None

    # event log に turn_failed for cohere_rerank が記録されてる
    log_file = tmp_path / "data" / "brain" / "bot_events" / "events.jsonl"
    assert log_file.exists()
    lines = log_file.read_text(encoding="utf-8").strip().splitlines()
    failures = [json.loads(l) for l in lines if "cohere_rerank" in l and "turn_failed" in l]
    assert len(failures) >= 1
    assert failures[0]["component"] == "cohere_rerank"


@pytest.mark.smoke
async def test_cohere_rerank_no_failure_event_when_no_key(tmp_path, monkeypatch):
    """COHERE_API_KEY 未設定なら graceful skip、event 発火しない (= 正常 skip 扱い)。"""
    monkeypatch.setenv("BRAIN_ROOT", str(tmp_path / "data" / "brain"))
    monkeypatch.delenv("COHERE_API_KEY", raising=False)

    from unittest.mock import AsyncMock
    http_mock = AsyncMock()

    from brain_wiki_helpers.rerank import cohere_rerank
    result = await cohere_rerank(query="test", documents=["d1"], http=http_mock)
    assert result is None
    # API 呼出無し
    http_mock.post.assert_not_called()
    # event 発火無し
    log_file = tmp_path / "data" / "brain" / "bot_events" / "events.jsonl"
    if log_file.exists():
        lines = log_file.read_text(encoding="utf-8").strip().splitlines()
        cohere_events = [l for l in lines if "cohere_rerank" in l]
        assert len(cohere_events) == 0


@pytest.mark.smoke
def test_drive_log_failure_helper(tmp_path, monkeypatch):
    """_log_drive_failure helper で turn_failed event 記録される。"""
    monkeypatch.setenv("BRAIN_ROOT", str(tmp_path / "data" / "brain"))
    from services.drive_ingest import _log_drive_failure
    _log_drive_failure("HttpError", "HTTP 500 server error")

    log_file = tmp_path / "data" / "brain" / "bot_events" / "events.jsonl"
    assert log_file.exists()
    lines = log_file.read_text(encoding="utf-8").strip().splitlines()
    failures = [json.loads(l) for l in lines if "drive_ingest" in l]
    assert len(failures) == 1
    assert failures[0]["component"] == "drive_ingest"
    assert failures[0]["event"] == "turn_failed"
    assert failures[0]["error_class"] == "HttpError"
