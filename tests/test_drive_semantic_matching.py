"""tests/test_drive_semantic_matching.py — Drive 検索の意味的関連 (★2026-05-26 海山指示)

旧: 「副業規定について教えて」 → keyword「副業, 規定」 → 「副業募集」 系も hit (意味違う)
新: Gemini に意図リフレーズ + 同義語 + 関連語 + 「意味的に合うものが無ければ空 array」
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


# ─── L1: expand_query prompt の意図リフレーズ ─────
@pytest.mark.asyncio
async def test_expand_query_prompt_has_intent_rephrase(gemini_mod, monkeypatch):
    """expand_query prompt に意図リフレーズ + 同義語 + 副業規定の例示が含まれる."""
    mod = gemini_mod
    captured = []

    async def fake_gen(prompt, response_json=False, max_tokens=200):
        captured.append(prompt)
        return json.dumps(["副業規程", "副業 就業規則", "副業 申請"])
    monkeypatch.setattr(mod, "_generate", fake_gen)

    await mod.expand_query("副業規定について教えて")
    assert captured
    prompt = captured[0]
    # 意図リフレーズの指示
    assert "意図" in prompt
    assert "意味" in prompt
    assert "同義語" in prompt or "関連語" in prompt
    # 副業規定 vs 副業募集 を区別する例示が prompt に
    assert "副業規定" in prompt
    assert "副業募集" in prompt or "副業バイト" in prompt
    # 「除外」 instruction (= 別文脈 keyword 含めない)
    assert "除外" in prompt


# ─── L2: rerank prompt の意味的関連 ─────
@pytest.mark.asyncio
async def test_rerank_prompt_has_semantic_match_instruction(gemini_mod, monkeypatch):
    """rerank prompt に意味的関連 instruction + 空 array OK 明記."""
    mod = gemini_mod
    captured = []

    async def fake_gen(prompt, response_json=False, max_tokens=400):
        captured.append(prompt)
        return json.dumps([])
    monkeypatch.setattr(mod, "_generate", fake_gen)

    files = [
        {"id": str(i), "name": f"f{i}", "modifiedTime": f"2026-05-{i:02d}T10:00:00Z"}
        for i in range(1, 6)
    ]
    await mod.rerank_results("副業規定について教えて", files, top_n=3)
    assert captured
    prompt = captured[0]
    # 意味的関連 instruction
    assert "意味的" in prompt
    assert "本質的" in prompt or "本質" in prompt
    # 副業規定 vs 副業募集 例示
    assert "副業規定" in prompt or "副業規程" in prompt
    assert "副業募集" in prompt or "副業バイト" in prompt
    # 空 array OK 明記
    assert "空 array" in prompt or "[]" in prompt
    assert "該当無し" in prompt or "該当なし" in prompt or "意味的に合致" in prompt


# ─── L3: Gemini が空 array → partial fill しない (= 「意味的該当無し」) ─────
@pytest.mark.asyncio
async def test_rerank_no_partial_fill_when_gemini_returns_empty(gemini_mod, monkeypatch):
    """Gemini が `[]` 返した時、partial fill しない (= 意味的該当無し を caller に伝える)."""
    mod = gemini_mod
    files = [
        {"id": str(i), "name": f"無関係 file {i}", "modifiedTime": f"2026-05-{i:02d}T10:00:00Z"}
        for i in range(1, 6)
    ]

    async def fake_gen(prompt, response_json=False, max_tokens=400):
        return json.dumps([])  # Gemini が「意味的に合うもの無い」 と判断
    monkeypatch.setattr(mod, "_generate", fake_gen)

    result = await mod.rerank_results("副業規定", files, top_n=3)
    assert result == [], f"Gemini が空返したら fill しないはず: {result}"


# ─── L4: Gemini が partial pick → ★2026-07-13 埋めない (fill 廃止) ─────
@pytest.mark.asyncio
async def test_rerank_partial_pick_no_fill(gemini_mod, monkeypatch):
    """Gemini が 1 件だけ選んだら 1 件だけ返す (★2026-07-13 海山指示: fill = 無理な提示 を廃止)。
    confidence: low の pick は rerank_confidence="low" で返る (表示側が参考扱い)。"""
    mod = gemini_mod
    files = [
        {"id": "1", "name": "old", "modifiedTime": "2026-01-01T10:00:00Z"},
        {"id": "2", "name": "newest_unselected", "modifiedTime": "2026-05-25T10:00:00Z"},
        {"id": "3", "name": "mid", "modifiedTime": "2026-03-15T10:00:00Z"},
        {"id": "4", "name": "ancient_unselected", "modifiedTime": "2025-08-01T10:00:00Z"},
        {"id": "5", "name": "extra", "modifiedTime": "2024-12-01T10:00:00Z"},
    ]

    async def fake_gen(prompt, response_json=False, max_tokens=400):
        return json.dumps([{"index": 1, "reason": "old picked", "confidence": "low"}])
    monkeypatch.setattr(mod, "_generate", fake_gen)

    result = await mod.rerank_results("test", files, top_n=3)
    assert len(result) == 1  # 埋めない
    assert result[0]["id"] == "1"
    assert result[0]["rerank_confidence"] == "low"


# ─── L5: source-level: _handle_drive_intent_query が semantic 0-case を扱う ─────
def test_handle_drive_intent_query_handles_semantic_no_match():
    """src grep: _handle_drive_intent_query が total_hits > 0 + top 0 の case で 該当無し message."""
    src = (REPO_ROOT / "main.py").read_text(encoding="utf-8")
    idx = src.find("async def _handle_drive_intent_query")
    assert idx > 0
    body = src[idx : idx + 5000]
    # total_hits > 0 だが top 0 の case 分岐
    assert "elif not result.get(\"top\")" in body or 'not result.get("top")' in body
    # 「該当無し」 系 message
    assert "意味" in body
    # 別言い回しでの再検索 suggestion
    assert "別の言い回し" in body or "別 keyword" in body or "別の keyword" in body
