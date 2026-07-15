"""tests/test_drive_rerank_recency.py — Drive rerank の 「最新優先」 tie-breaker test

★2026-05-26 海山指示 (= A+B 推奨):
A. prompt instruction で 「同関連度なら最新」 hint
B. rerank 後 (_gemini_rank, -modifiedTime_unix) で 二次 sort
C. partial fill (= Gemini が top_n より少ない pick → 残りを modifiedTime DESC で埋める)
"""
from __future__ import annotations

import importlib
import json
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))


@pytest.fixture
def gemini_mod(monkeypatch):
    monkeypatch.setenv("GEMINI_API_KEY", "fake")
    if "services.gemini_query" in sys.modules:
        del sys.modules["services.gemini_query"]
    return importlib.import_module("services.gemini_query")


def test_iso_to_unix(gemini_mod):
    mod = gemini_mod
    assert mod._iso_to_unix("2026-05-26T10:00:00Z") > 0
    # 新しい方が大きい unix timestamp
    new_ts = mod._iso_to_unix("2026-05-26T10:00:00Z")
    old_ts = mod._iso_to_unix("2025-01-01T10:00:00Z")
    assert new_ts > old_ts
    # invalid
    assert mod._iso_to_unix("") == 0.0
    assert mod._iso_to_unix("invalid") == 0.0
    assert mod._iso_to_unix(None) == 0.0


@pytest.mark.asyncio
async def test_rerank_no_partial_fill(gemini_mod, monkeypatch):
    """★2026-07-13 海山指示「関連したものが見つからない場合は、無理に提示する必要がない」:
    Gemini が top_n より少ない pick → **埋めない** (旧: modifiedTime DESC で残り埋め =
    無関係 file の水増し提示だったため廃止)。確信のある分だけ返す。"""
    mod = gemini_mod
    files = [
        {"id": "1", "name": "old", "modifiedTime": "2026-01-01T10:00:00Z"},
        {"id": "2", "name": "newest_unselected", "modifiedTime": "2026-05-25T10:00:00Z"},
        {"id": "3", "name": "mid", "modifiedTime": "2026-03-15T10:00:00Z"},
        {"id": "4", "name": "ancient_unselected", "modifiedTime": "2025-08-01T10:00:00Z"},
        {"id": "5", "name": "extra", "modifiedTime": "2024-12-01T10:00:00Z"},
    ]

    async def fake_gen(prompt, response_json=False, max_tokens=400):
        # Gemini が 2 件しか返さない: file 1 (old) と file 3 (mid)
        return json.dumps([
            {"index": 1, "reason": "selected old"},
            {"index": 3, "reason": "selected mid"},
        ])
    monkeypatch.setattr(mod, "_generate", fake_gen)

    result = await mod.rerank_results("test", files, top_n=3)
    assert len(result) == 2  # 埋めない
    assert result[0]["id"] == "1" and result[1]["id"] == "3"
    # confidence 未指定は high 扱い (後方互換)
    assert all(f.get("rerank_confidence") == "high" for f in result)


@pytest.mark.asyncio
async def test_rerank_no_internal_field_leak(gemini_mod, monkeypatch):
    """_gemini_rank は caller に返す result から削除されてる"""
    mod = gemini_mod
    files = [
        {"id": str(i), "name": f"f{i}", "modifiedTime": f"2026-05-{i:02d}T10:00:00Z"}
        for i in range(1, 6)
    ]
    async def fake_gen(prompt, response_json=False, max_tokens=400):
        return json.dumps([{"index": i, "reason": f"r{i}"} for i in range(1, 4)])
    monkeypatch.setattr(mod, "_generate", fake_gen)

    result = await mod.rerank_results("test", files, top_n=3)
    for f in result:
        assert "_gemini_rank" not in f  # internal field 漏れ無し
        assert "rerank_reason" in f
        assert "id" in f


@pytest.mark.asyncio
async def test_rerank_dedupe_on_duplicate_index(gemini_mod, monkeypatch):
    """Gemini が誤って同 index 重複返した時、dedupe で 1 件のみに"""
    mod = gemini_mod
    files = [
        {"id": "1", "name": "f1", "modifiedTime": "2026-05-01T10:00:00Z"},
        {"id": "2", "name": "f2", "modifiedTime": "2026-04-01T10:00:00Z"},
        {"id": "3", "name": "f3", "modifiedTime": "2026-03-01T10:00:00Z"},
    ]
    async def fake_gen(prompt, response_json=False, max_tokens=400):
        return json.dumps([
            {"index": 1, "reason": "A"},
            {"index": 1, "reason": "duplicate"},  # dup
            {"index": 2, "reason": "B"},
        ])
    monkeypatch.setattr(mod, "_generate", fake_gen)

    result = await mod.rerank_results("test", files, top_n=3)
    ids = [f["id"] for f in result]
    assert ids.count("1") == 1  # dedupe 後 1 回のみ
    # 残り (= top_n=3 だが 2 unique selected) は modifiedTime DESC で fill
    # remaining = [3], newer file は無いので file 3 が fill
    assert "3" in ids


@pytest.mark.asyncio
async def test_rerank_prompt_includes_recency_instruction(gemini_mod, monkeypatch):
    """prompt に「modifiedTime が新しい方を優先」 instruction が含まれてる"""
    mod = gemini_mod
    captured_prompt = []
    async def fake_gen(prompt, response_json=False, max_tokens=400):
        captured_prompt.append(prompt)
        return json.dumps([])
    monkeypatch.setattr(mod, "_generate", fake_gen)

    files = [
        {"id": str(i), "name": f"f{i}", "modifiedTime": f"2026-05-{i:02d}T10:00:00Z"}
        for i in range(1, 5)
    ]
    await mod.rerank_results("test", files, top_n=2)
    assert len(captured_prompt) == 1
    prompt = captured_prompt[0]
    # 「modifiedTime」 + 「新しい」 が prompt に含まれる
    assert "modifiedTime" in prompt
    assert "新しい" in prompt or "最新" in prompt


@pytest.mark.asyncio
async def test_rerank_fallback_still_modtime_desc(gemini_mod, monkeypatch):
    """Gemini 失敗時の fallback は依然 modifiedTime DESC"""
    mod = gemini_mod
    files = [
        {"id": "1", "name": "old", "modifiedTime": "2026-01-01T10:00:00Z"},
        {"id": "2", "name": "new", "modifiedTime": "2026-05-25T10:00:00Z"},
        {"id": "3", "name": "mid", "modifiedTime": "2026-03-01T10:00:00Z"},
    ]
    async def fake_gen(prompt, response_json=False, max_tokens=400):
        raise mod.GeminiUnavailableError("network")
    monkeypatch.setattr(mod, "_generate", fake_gen)

    result = await mod.rerank_results("test", files, top_n=2)
    assert result[0]["id"] == "2"  # newest first
    assert result[1]["id"] == "3"
