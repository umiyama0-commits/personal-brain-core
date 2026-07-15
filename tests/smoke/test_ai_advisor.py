"""smoke: scripts/ai_advisor.py — synthesize(注入 llm)/ emit_wiki(public static-factual)/
build_push / dry-run。network・LLM 非依存(全て注入 or monkeypatch)。"""
import asyncio
import pathlib
import sys

ROOT = pathlib.Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "scripts"))   # script-mode(本番と同じ sibling import)

import ai_advisor  # noqa: E402


def test_synthesize_uses_injected_llm_and_business_guards():
    seen = {}

    async def fake_llm(prompt, **k):
        seen["prompt"] = prompt
        return "## 直近 pilot top 3\n### 1. 店頭AI接客\n- OWNDAYS 接点: 店舗\n- 試せる打ち手: pilot"

    out = asyncio.run(ai_advisor.synthesize(
        [{"title": "P", "summary": "s", "link": "u"}], [], [], llm=fake_llm))
    assert "pilot" in out
    # ビジネス文脈 + 捏造禁止ガードが prompt に入っている
    assert "OWNDAYS" in seen["prompt"]
    assert "捏造禁止" in seen["prompt"] and "数値" in seen["prompt"]


def test_build_push_header_and_clone_pointer():
    p = ai_advisor.build_push("## 直近 pilot top 3\n### 1. 店舗AI\n- 接点: 店舗")
    assert "AI 活用提言" in p and "うみやまAI" in p


def test_emit_wiki_private_static_factual(monkeypatch):
    captured = {}

    def fake_bw(**kw):
        captured.update(kw)
        return pathlib.Path("/tmp/ai-trends-owndays.md")

    monkeypatch.setattr(ai_advisor, "build_analysis_wiki", fake_bw)
    ai_advisor.emit_wiki("body text", ["arxiv"])
    assert captured["pj_id"] == "ai-trends-owndays"
    assert captured["pj_class"] == "static-factual"          # public 可(model-estimate は public 不可)
    assert captured["visibility"] == "private"   # ★2026-06-21 security: web 由来合成は private(clone 非引用)
    assert captured["valid_days"] == 14                       # 自動失効(staleness 対策)


def test_run_dry_run_no_wiki_no_push(monkeypatch):
    async def empty(*a, **k):
        return []

    monkeypatch.setattr(ai_advisor, "fetch_arxiv_recent", empty)
    monkeypatch.setattr(ai_advisor, "fetch_hn_ai_stories", empty)
    monkeypatch.setattr(ai_advisor, "fetch_vendor_news", empty)

    async def fake_llm(prompt, **k):
        return ("## 直近 pilot top 3\n### 1. 店頭AI接客\n- 新規性: マルチモーダルで接客補助の可能性(要検証)\n"
                "- OWNDAYS 接点: 店舗\n- 試せる打ち手: 1店舗で小さく pilot\n- 確度: 中 / 難易度: 小\n"
                "## watch list\n- 検眼AI: 様子見")

    r = asyncio.run(ai_advisor.run(dry_run=True, llm=fake_llm))
    assert r["ok"] is True and r.get("dry_run") is True
    assert "wiki" not in r and "pushed" not in r              # dry-run は副作用なし


def test_run_writes_wiki_and_pushes_when_enabled(monkeypatch):
    async def empty(*a, **k):
        return []

    monkeypatch.setattr(ai_advisor, "fetch_arxiv_recent", empty)
    monkeypatch.setattr(ai_advisor, "fetch_hn_ai_stories", empty)
    monkeypatch.setattr(ai_advisor, "fetch_vendor_news", empty)
    monkeypatch.setattr(ai_advisor, "build_analysis_wiki",
                        lambda **kw: pathlib.Path("/tmp/ai-trends-owndays.md"))
    pushed = {}

    async def fake_llm(prompt, **k):
        return ("## 直近 pilot top 3\n### 1. 店頭AI接客\n- 新規性: マルチモーダルで接客補助の可能性(要検証)\n"
                "- OWNDAYS 接点: 店舗\n- 試せる打ち手: 1店舗で小さく pilot\n- 確度: 中 / 難易度: 小\n"
                "## watch list\n- 検眼AI: 様子見")

    r = asyncio.run(ai_advisor.run(
        llm=fake_llm, push_fn=lambda t: pushed.setdefault("t", t) or True))
    assert r["ok"] and r["wiki"].endswith("ai-trends-owndays.md") and r["pushed"] is True
    assert "AI 活用提言" in pushed["t"]
