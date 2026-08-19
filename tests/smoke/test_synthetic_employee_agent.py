"""smoke test: synthetic_employee_agent — 安全境界の検証 (live LLM/docker 不要)

★2026-06-07 海山指示の synthetic 社員エージェント (cross-check 反映 + Phase1b alias レバー):
- 仮想環境隔離: 会話を一切 persist しない (clone_history/memory を汚さない)
- 唯一の自律 write = drive_search_aliases.json への findability alias (事実/prompt 不介入)
  。AUTOFIX off / alias 無 / budget 0 では queue に fallback
- 全カテゴリ queue + content-hash dedup (= /admin/review を重複で埋めない)
- 系列分離 guard (bot=judge の self-eval loop 退化を起動時に阻止)
- dry_run は bot を叩かない / MAX_QUERIES で cost cap / 生成全滅は degraded 報告
"""
from __future__ import annotations

import asyncio
import importlib
import json
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent.parent
for _p in (str(REPO), str(REPO / "scripts")):
    if _p not in sys.path:
        sys.path.insert(0, _p)


@pytest.fixture
def agent(monkeypatch):
    monkeypatch.setenv("LITELLM_MASTER_KEY", "x")
    import scripts.synthetic_employee_agent as a
    importlib.reload(a)
    return a


def _fake_clone_learning(monkeypatch):
    fake = type(sys)("clone_learning")
    fake.add_manual_entry = lambda insight, proposed_wiki_patch="", reviewer="": "cl_test"
    monkeypatch.setitem(sys.modules, "clone_learning", fake)


def _fake_system_issues(monkeypatch):
    fake = type(sys)("services.system_issues")
    fake.add_entry = lambda description, expected="", reviewer="": "sysi_test"
    monkeypatch.setitem(sys.modules, "services.system_issues", fake)


@pytest.mark.smoke
def test_no_conversation_persistence_in_source():
    """仮想環境隔離 + dead-write 撤去: clone_history/memory を import せず、reindex も
    旧 patches.json 自律 write もしない。唯一の自律 write は alias file。"""
    src = (REPO / "scripts" / "synthetic_employee_agent.py").read_text(encoding="utf-8")
    assert "import clone_history" not in src and "from clone_history" not in src
    assert "import clone_memory" not in src and "from clone_memory" not in src
    assert "reindex_history" not in src, "chromadb reindex を呼ばない (§1.5)"
    assert "_apply_keyword_autonomous" not in src, "旧 dead-write 関数は撤去済"
    assert "PATCHES_PATH" not in src, "patches.json への自律 write は無い"
    assert "ALIASES_PATH" in src, "唯一の自律 write 先 = alias file"


@pytest.mark.smoke
def test_keyword_alias_proposed_pending(agent, monkeypatch, tmp_path):
    """★Phase1b verify-before-activate: AUTOFIX on で alias を enabled=False (未承認) で記録。
    承認するまで検索には効かない (= 誤リンク再生産の遮断)。"""
    monkeypatch.setattr(agent, "AUTOFIX", True)
    monkeypatch.setattr(agent, "ALIASES_PATH", tmp_path / "aliases.json")
    diag = {"has_issue": True, "fix_category": "keyword", "issue_type": "keyword_miss",
            "alias_term": "クリエイトリンク", "alias_synonyms": ["包括出店PJ"], "proposed_fix": "x"}
    r = agent.route(diag, agent.PERSONAS[0], "クリエイトリンクって何", "resp", seen={}, autofix_remaining=3)
    assert r["action"] == "proposed_alias"
    data = json.loads((tmp_path / "aliases.json").read_text(encoding="utf-8"))
    assert "包括出店PJ" in data["クリエイトリンク"]["aliases"]
    assert data["クリエイトリンク"]["enabled"] is False, "未承認で記録 (検索に効かない)"


@pytest.mark.smoke
def test_approve_alias(agent, monkeypatch, tmp_path):
    """海山承認で enabled=True、取消で False、存在しない term は False。"""
    af = tmp_path / "aliases.json"
    monkeypatch.setattr(agent, "ALIASES_PATH", af)
    af.write_text(json.dumps({"クリエイトリンク": {"aliases": ["包括出店PJ"], "enabled": False}},
                             ensure_ascii=False), encoding="utf-8")
    assert agent.approve_alias("クリエイトリンク", True) is True
    assert json.loads(af.read_text(encoding="utf-8"))["クリエイトリンク"]["enabled"] is True
    assert agent.approve_alias("クリエイトリンク", False) is True
    assert json.loads(af.read_text(encoding="utf-8"))["クリエイトリンク"]["enabled"] is False
    assert agent.approve_alias("存在しない", True) is False


@pytest.mark.smoke
def test_keyword_no_alias_queues(agent, monkeypatch):
    """alias_synonyms が無ければ AUTOFIX on でも自律追記せず queue (= 連想で勝手に作らない)。"""
    monkeypatch.setattr(agent, "AUTOFIX", True)
    _fake_clone_learning(monkeypatch)
    diag = {"has_issue": True, "fix_category": "keyword", "issue_type": "keyword_miss",
            "alias_term": "", "alias_synonyms": [], "proposed_fix": "x"}
    r = agent.route(diag, agent.PERSONAS[0], "q", "resp", seen={}, autofix_remaining=3)
    assert r["action"] == "queued_keyword"


@pytest.mark.smoke
def test_keyword_alias_autofix_off_queues(agent, monkeypatch):
    """AUTOFIX off なら確実な alias があっても queue (= Phase 0 は完全 propose-only)。"""
    monkeypatch.setattr(agent, "AUTOFIX", False)
    _fake_clone_learning(monkeypatch)
    diag = {"has_issue": True, "fix_category": "keyword", "issue_type": "keyword_miss",
            "alias_term": "X", "alias_synonyms": ["Y"], "proposed_fix": "x"}
    r = agent.route(diag, agent.PERSONAS[0], "q", "resp", seen={}, autofix_remaining=3)
    assert r["action"] == "queued_keyword"


@pytest.mark.smoke
def test_alias_autofix_budget_exhausted_queues(agent, monkeypatch, tmp_path):
    """残枠 0 なら queue に fallback (= MAX_AUTOFIX cap 厳守)。"""
    monkeypatch.setattr(agent, "AUTOFIX", True)
    monkeypatch.setattr(agent, "ALIASES_PATH", tmp_path / "a.json")
    _fake_clone_learning(monkeypatch)
    diag = {"has_issue": True, "fix_category": "keyword", "issue_type": "keyword_miss",
            "alias_term": "X", "alias_synonyms": ["Y"], "proposed_fix": "x"}
    r = agent.route(diag, agent.PERSONAS[0], "q", "resp", seen={}, autofix_remaining=0)
    assert r["action"] == "queued_keyword"


@pytest.mark.smoke
def test_append_drive_alias_dedup_and_guards(agent, monkeypatch, tmp_path):
    """alias 記録: merge + dedup、変化なし/空/短 term は False (事実不介入 + 誤爆防止ガード)。"""
    monkeypatch.setattr(agent, "ALIASES_PATH", tmp_path / "a.json")
    assert agent._append_drive_alias("案件A", ["別名Y", "別名Z"], "q") is True
    assert agent._append_drive_alias("案件A", ["別名Y", "別名Z"], "q") is False  # 変化なし
    assert agent._append_drive_alias("案件A", ["別名W"], "q") is True            # 新 synonym 追加
    data = json.loads((tmp_path / "a.json").read_text(encoding="utf-8"))
    assert set(data["案件A"]["aliases"]) == {"別名Y", "別名Z", "別名W"}
    assert data["案件A"]["enabled"] is False                                     # 未承認で記録
    assert agent._append_drive_alias("案件A", [], "q") is False                  # synonym 無
    assert agent._append_drive_alias("", ["別名Y"], "q") is False                # term 無
    assert agent._append_drive_alias("P", ["別名Y"], "q") is False               # 短 term (誤爆源) 拒否


@pytest.mark.smoke
def test_route_wiki_content_queues(agent, monkeypatch):
    """事実 wiki content は queue (= 診断 LLM は ground truth 不在で誤りうる)。"""
    _fake_clone_learning(monkeypatch)
    diag = {"has_issue": True, "fix_category": "wiki_content", "severity": "medium",
            "proposed_fix": "売上は X", "wiki_target": "knowledge/y.md"}
    r = agent.route(diag, agent.PERSONAS[0], "q", "resp", seen={})
    assert r["action"] == "queued_wiki_content"


@pytest.mark.smoke
def test_route_high_risk_queues_system(agent, monkeypatch):
    """prompt/retrieval/code は system_issues queue (自己適用しない)。"""
    _fake_system_issues(monkeypatch)
    for cat in ("prompt", "retrieval", "code"):
        diag = {"has_issue": True, "fix_category": cat, "severity": "high",
                "root_cause": "x", "proposed_fix": "y"}
        r = agent.route(diag, agent.PERSONAS[0], "q", "resp", seen={})
        assert r["action"] == "queued_system", f"{cat} は system_issues queue"


@pytest.mark.smoke
def test_dedup_skips_duplicate(agent, monkeypatch):
    """content-hash dedup: 同義 issue の2回目は queue せず deduped。"""
    _fake_clone_learning(monkeypatch)
    diag = {"has_issue": True, "fix_category": "wiki_content", "proposed_fix": "売上 X"}
    seen: dict = {}
    r1 = agent.route(diag, agent.PERSONAS[0], "q1", "resp", seen)
    r2 = agent.route(diag, agent.PERSONAS[0], "q2", "resp", seen)
    assert r1["action"] == "queued_wiki_content"
    assert r2["action"] == "deduped"


@pytest.mark.smoke
def test_model_family_guard_aborts(agent, monkeypatch):
    """bot と 生成/診断 が同一 model なら self-eval loop → 起動時に中止 (§1.15)。"""
    monkeypatch.setattr(agent, "BOT_MODEL", "smart")
    monkeypatch.setattr(agent, "GEN_MODEL", "smart")
    out = asyncio.run(agent.run_all(dry_run=True, push=False))
    assert out.get("error") == "model_family_collision"


@pytest.mark.smoke
def test_dry_run_does_not_call_bot(agent, monkeypatch):
    """dry_run: query 生成のみ、bot を叩かない。"""
    async def fake_gen(persona, n):
        return [f"q{i}" for i in range(n)]
    monkeypatch.setattr(agent, "generate_queries", fake_gen)

    async def boom(q):
        raise AssertionError("dry_run で run_bot を呼んではいけない")
    monkeypatch.setattr(agent, "run_bot", boom)

    out = asyncio.run(agent.run_all(sample=1, dry_run=True, push=False))
    assert out["dry_run"] is True and out["n_queries"] > 0


@pytest.mark.smoke
def test_max_queries_cap(agent, monkeypatch):
    """MAX_QUERIES で総質問数を cap (cost 暴走防止)。"""
    monkeypatch.setattr(agent, "MAX_QUERIES", 3)

    async def fake_gen(persona, n):
        return [f"q{i}" for i in range(n)]
    monkeypatch.setattr(agent, "generate_queries", fake_gen)

    async def boom(q):
        raise AssertionError("dry_run")
    monkeypatch.setattr(agent, "run_bot", boom)

    out = asyncio.run(agent.run_all(sample=None, dry_run=True, push=False))
    assert out["n_queries"] == 3


@pytest.mark.smoke
def test_degraded_when_no_queries(agent, monkeypatch):
    """生成全滅 (LLM 不調) を『異常なし』と誤報告せず degraded を立てる。"""
    async def empty_gen(persona, n):
        return []
    monkeypatch.setattr(agent, "generate_queries", empty_gen)

    async def boom(q):
        raise AssertionError("bot を叩いてはいけない")
    monkeypatch.setattr(agent, "run_bot", boom)

    out = asyncio.run(agent.run_all(sample=1, dry_run=False, push=False))
    assert out.get("degraded") == "no_queries_generated"


@pytest.mark.smoke
def test_disabled_kill_switch(agent, monkeypatch):
    """SYNTHETIC_AGENT_ENABLED=0 で全停止。"""
    monkeypatch.setattr(agent, "ENABLED", False)
    out = asyncio.run(agent.run_all(dry_run=True, push=False))
    assert out.get("skipped") == "disabled"
