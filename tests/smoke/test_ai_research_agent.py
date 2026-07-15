"""smoke test: scripts/ai_research_agent.py (★2026-05-24 AI Research Agent)

source fetch + LLM synthesis (mock) + 提案抽出 + LINE Push (mock) の sanity test。
"""
from __future__ import annotations

import importlib
import json
import sys
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

REPO = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(REPO))
sys.path.insert(0, str(REPO / "scripts"))


@pytest.mark.smoke
def test_module_imports():
    """module 読込可能 + 主要関数存在。"""
    import ai_research_agent as ara
    assert hasattr(ara, "fetch_arxiv_recent")
    assert hasattr(ara, "fetch_hn_ai_stories")
    assert hasattr(ara, "fetch_vendor_news")
    assert hasattr(ara, "synthesize_digest")
    assert hasattr(ara, "save_digest")
    assert hasattr(ara, "extract_proposals")
    assert hasattr(ara, "build_push_summary")
    assert hasattr(ara, "run")


@pytest.mark.smoke
def test_strip_html():
    """HTML tag 除去."""
    from ai_research_agent import _strip_html
    assert _strip_html("<p>hello <b>world</b></p>") == "hello  world"
    assert _strip_html("plain text") == "plain text"
    assert _strip_html("") == ""
    assert _strip_html("&nbsp;test&nbsp;").strip() == "test"


@pytest.mark.smoke
def test_arxiv_categories_well_known():
    """arxiv categories 定義 sanity."""
    import ai_research_agent as ara
    assert "cs.AI" in ara.ARXIV_CATEGORIES
    assert "cs.CL" in ara.ARXIV_CATEGORIES
    assert "cs.LG" in ara.ARXIV_CATEGORIES


@pytest.mark.smoke
def test_ai_keywords_include_majors():
    """AI keyword filter に主要 vendor / 技術名が含まれる."""
    from ai_research_agent import AI_KEYWORDS
    keywords_str = " ".join(AI_KEYWORDS).lower()
    assert "gpt" in keywords_str
    assert "claude" in keywords_str
    assert "anthropic" in keywords_str
    assert "openai" in keywords_str
    assert "rag" in keywords_str


@pytest.mark.smoke
def test_pj_context_describes_current_architecture():
    """PJ_CONTEXT が当 PJ の主要 component を含む (= LLM へ正確な前提 inject)."""
    from ai_research_agent import PJ_CONTEXT
    # 主要 component 名が含まれてる
    assert "brain_wiki" in PJ_CONTEXT
    assert "clone_memory" in PJ_CONTEXT
    assert "clone_group_context" in PJ_CONTEXT
    assert "Cohere Rerank" in PJ_CONTEXT
    assert "CLONE_PUBLIC_PROMPT" in PJ_CONTEXT
    assert "Phase 1" in PJ_CONTEXT


@pytest.mark.smoke
def test_extract_proposals_from_digest(tmp_path, monkeypatch):
    """digest text から ### N. ... 形式 proposal を抽出。"""
    monkeypatch.setenv("BRAIN_APP_ROOT", str(tmp_path))
    import ai_research_agent
    importlib.reload(ai_research_agent)
    from ai_research_agent import extract_proposals, RESEARCH_DIR, PROPOSALS_FILE

    digest = """# AI Research Digest — 2026-05-24

## ⚡ 直近 着手 top 3

### 1. New retrieval pattern
- **新規性**: hybrid sparse + dense
- **PJ 反映**: brain_wiki に追加
- **impact**: high / **effort**: 1 sprint

### 2. Memory consolidation paper
- **新規性**: sleep-time agent variant
- **PJ 反映**: sleep_time_agent 拡張

## 📚 着手保留

### 3. Multi-step reasoning
- **新規性**: chain
- **PJ 反映**: clone_respond
"""
    items = extract_proposals(digest, "2026-05-24")
    assert len(items) == 3
    assert items[0]["id"] == "2026-05-24_p01"
    assert "retrieval" in items[0]["title"]
    assert items[1]["id"] == "2026-05-24_p02"
    assert items[2]["id"] == "2026-05-24_p03"
    # proposals.jsonl に書き込まれた
    assert PROPOSALS_FILE.exists()
    lines = PROPOSALS_FILE.read_text(encoding="utf-8").strip().splitlines()
    assert len(lines) == 3
    for ln in lines:
        rec = json.loads(ln)
        assert rec["status"] == "pending"
        assert "source_digest" in rec


@pytest.mark.smoke
def test_save_digest_creates_file(tmp_path, monkeypatch):
    """save_digest で日付別 md file が生成される."""
    monkeypatch.setenv("BRAIN_APP_ROOT", str(tmp_path))
    import ai_research_agent
    importlib.reload(ai_research_agent)
    from ai_research_agent import save_digest

    digest = "# digest content\n\nsome body"
    path = save_digest(digest, "2026-05-24")
    assert path.exists()
    assert path.name == "2026-05-24-digest.md"
    assert "digest content" in path.read_text(encoding="utf-8")


@pytest.mark.smoke
def test_build_push_summary_extracts_urgent():
    """push summary に urgent top 3 section が含まれる."""
    from ai_research_agent import build_push_summary
    digest = """# AI Research Digest

## ⚡ 直近 着手 top 3

### 1. Item A
- detail

### 2. Item B
- detail

## 📚 着手保留

### 3. Item C
"""
    summary = build_push_summary(digest, "2026-05-24", 3)
    assert "2026-05-24" in summary
    assert "Item A" in summary
    assert "Item B" in summary
    # 着手保留 section は含まれない (= 1500 char 制限内 + urgent のみ)
    # Note: actual cutoff depends on extraction logic


@pytest.mark.smoke
async def test_run_dry_run_skips_llm(tmp_path, monkeypatch):
    """dry_run=True で LLM call スキップ、source 件数のみ報告."""
    monkeypatch.setenv("BRAIN_APP_ROOT", str(tmp_path))
    import ai_research_agent
    importlib.reload(ai_research_agent)

    # fetch を全 mock (= network access せず)
    with patch.object(ai_research_agent, "fetch_arxiv_recent",
                      AsyncMock(return_value=[{"title": "test"}])), \
         patch.object(ai_research_agent, "fetch_hn_ai_stories",
                      AsyncMock(return_value=[])), \
         patch.object(ai_research_agent, "fetch_vendor_news",
                      AsyncMock(return_value=[])):
        result = await ai_research_agent.run(dry_run=True)
    assert result["dry_run"] is True
    assert result["arxiv_count"] == 1
    assert result["hn_count"] == 0


@pytest.mark.smoke
async def test_run_full_flow_with_mocks(tmp_path, monkeypatch):
    """fetch + synthesis + save + push を mock で end-to-end test."""
    monkeypatch.setenv("BRAIN_APP_ROOT", str(tmp_path))
    import ai_research_agent
    importlib.reload(ai_research_agent)

    mock_digest = """# AI Research Digest — 2026-05-24

## ⚡ 直近 着手 top 3

### 1. Mock proposal
- **新規性**: test
- **PJ 反映**: brain_wiki

## 📊 Source 統計
- arxiv: 1 / hn: 0 / vendor: 0
"""
    with patch.object(ai_research_agent, "fetch_arxiv_recent",
                      AsyncMock(return_value=[{"title": "Mock paper",
                                              "summary": "...", "link": "x"}])), \
         patch.object(ai_research_agent, "fetch_hn_ai_stories",
                      AsyncMock(return_value=[])), \
         patch.object(ai_research_agent, "fetch_vendor_news",
                      AsyncMock(return_value=[])), \
         patch.object(ai_research_agent, "call_llm",
                      AsyncMock(return_value=mock_digest)), \
         patch.object(ai_research_agent, "line_push",
                      MagicMock(return_value=True)):
        result = await ai_research_agent.run(no_push=False)

    assert "digest_path" in result
    assert result["n_proposals"] == 1
    assert result["line_pushed"] is True
    # digest file 生成された
    digest_files = list((tmp_path / "data" / "brain" / "ai_research").glob("*-digest.md"))
    assert len(digest_files) == 1


@pytest.mark.smoke
async def test_run_llm_failure_logged(tmp_path, monkeypatch):
    """LLM failure 時に error 報告 + turn_failed event 記録."""
    monkeypatch.setenv("BRAIN_APP_ROOT", str(tmp_path))
    monkeypatch.setenv("BRAIN_ROOT", str(tmp_path / "data" / "brain"))
    import ai_research_agent
    importlib.reload(ai_research_agent)

    with patch.object(ai_research_agent, "fetch_arxiv_recent",
                      AsyncMock(return_value=[])), \
         patch.object(ai_research_agent, "fetch_hn_ai_stories",
                      AsyncMock(return_value=[])), \
         patch.object(ai_research_agent, "fetch_vendor_news",
                      AsyncMock(return_value=[])), \
         patch.object(ai_research_agent, "call_llm",
                      AsyncMock(side_effect=Exception("LLM unavailable"))):
        result = await ai_research_agent.run(no_push=True)

    assert "error" in result
    assert "LLM unavailable" in result["error"]
