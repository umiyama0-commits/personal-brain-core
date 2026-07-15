"""smoke test: Phase 0 wiki access 集計 (ADR 2026-06-01 tiered-memory)

wiki_access_aggregate.aggregate() のロジック検証:
- retrieval/wiki_context イベントのみ集計 (他 component は無視)
- 時間減衰 (half-life) が効く (古い採用ほど軽い)
- 生回数 / context size 分布 / intent 分布
実 LLM・実 bot 不要、合成イベントで純粋に検証。
"""
from __future__ import annotations

import json
from datetime import datetime, timedelta

import pytest


def _write_events(tmp_path, lines):
    ev_dir = tmp_path / "bot_events"
    ev_dir.mkdir(parents=True, exist_ok=True)
    (ev_dir / "events.jsonl").write_text("\n".join(lines), encoding="utf-8")


@pytest.mark.smoke
def test_aggregate_decay_and_filtering(tmp_path):
    from scripts.wiki_access_aggregate import aggregate

    now = datetime(2026, 6, 1, 12, 0, 0)
    old = now - timedelta(days=60)  # half-life 30 → weight 0.5^2 = 0.25
    lines = [
        json.dumps({"ts": now.isoformat(), "component": "retrieval", "event": "wiki_context",
                    "query_intent": "business", "total_chars": 50000,
                    "recall": ["knowledge/a.md", "knowledge/b.md"]}),
        json.dumps({"ts": old.isoformat(), "component": "retrieval", "event": "wiki_context",
                    "query_intent": "sales", "total_chars": 70000,
                    "recall": ["knowledge/a.md"]}),
        # 別 component は集計対象外
        json.dumps({"ts": now.isoformat(), "component": "clone_respond", "event": "turn_finished"}),
    ]
    _write_events(tmp_path, lines)

    out = aggregate(tmp_path, half_life_days=30.0, since_days=90.0, now=now, write=False)

    assert out["n_turns"] == 2, "retrieval/wiki_context のみ数えるべき (clone_respond は除外)"
    assert out["recall_raw"]["knowledge/a.md"] == 2
    assert out["recall_raw"]["knowledge/b.md"] == 1
    # A = 最近(1.0) + 60日前(0.25) = 1.25 > B = 最近のみ(1.0)
    assert out["recall_decayed"]["knowledge/a.md"] > out["recall_decayed"]["knowledge/b.md"]
    assert abs(out["recall_decayed"]["knowledge/a.md"] - 1.25) < 0.01
    assert out["intent_distribution"] == {"business": 1, "sales": 1}
    assert out["context_chars_p50"] in (50000, 70000)
    assert out["context_chars_p95"] == 70000


@pytest.mark.smoke
def test_aggregate_since_filter_excludes_old(tmp_path):
    from scripts.wiki_access_aggregate import aggregate

    now = datetime(2026, 6, 1, 12, 0, 0)
    too_old = now - timedelta(days=200)  # since_days=90 → 除外
    lines = [
        json.dumps({"ts": too_old.isoformat(), "component": "retrieval", "event": "wiki_context",
                    "query_intent": "general", "total_chars": 40000, "recall": ["knowledge/z.md"]}),
    ]
    _write_events(tmp_path, lines)
    out = aggregate(tmp_path, since_days=90.0, now=now, write=False)
    assert out["n_turns"] == 0
    assert out["recall_decayed"] == {}


@pytest.mark.smoke
def test_aggregate_handles_missing_events(tmp_path):
    """events.jsonl が無くても crash しない (空集計を返す)。"""
    from scripts.wiki_access_aggregate import aggregate

    out = aggregate(tmp_path, now=datetime(2026, 6, 1), write=False)
    assert out["n_turns"] == 0
    assert out["recall_decayed"] == {}
    assert out["context_chars_p50"] == 0
