"""smoke: scripts/reflux.py — 還流 (各PJ→Core 蒸留 + 海山承認) の安全不変条件 (★2026-06-28 Step 2)。

network・LLM 非依存 (llm 注入 + tmp wiki)。検証する不変条件:
- 機密 decisions は蒸留入力から除外 (§1.9 + 法務/incident)
- 蒸留→queue は propose-only、承認まで Core 不変
- 承認は出所引用を検証 (捏造は block)、Core は private で書かれる
- 二重承認 no-op (冪等)、dedup (再 run で重複追加せず)
"""
import asyncio
import pathlib
import sys

ROOT = pathlib.Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "scripts"))

import reflux  # noqa: E402


def _seed(tmp_path):
    wiki = tmp_path / "wiki"
    (wiki / "decisions").mkdir(parents=True)
    (wiki / "personal" / "example-garden").mkdir(parents=True)
    (wiki / "judgment").mkdir(parents=True)
    (wiki / "decisions" / "2026-06-20-employee-termination-strategy.md").write_text(
        "# 解雇方針\n機密 CONFIDENTIAL_LEAK。退職交渉の進め方。" * 5, encoding="utf-8")
    plan = ("# Example Garden 計画\n現場の声を起点に意思決定する、という原則をExampleで再確認した。"
            "初期は仮説を小さく検証し、数字が出てから投資を厚くする。撤退基準を事前に決める。") * 3
    (wiki / "personal" / "example-garden" / "plan.md").write_text(plan, encoding="utf-8")
    reflux.WIKI_DIR = wiki
    reflux.QUEUE = tmp_path / "reflux_queue.jsonl"
    reflux.CORE_TARGET = wiki / "judgment" / "reflux-distilled.md"
    return wiki


async def _fake_llm(prompt, **k):
    return ('{"candidates":[{"principle":"現場の声を意思決定の起点に置く","type":"judgment",'
            '"evidence_file":"personal/example-garden/plan.md",'
            '"evidence_quote":"現場の声を起点に意思決定する","generalizable_note":"どの事業でも通じる"}]}')


def test_sensitive_decision_excluded_from_distill(tmp_path):
    _seed(tmp_path)
    assert "CONFIDENTIAL_LEAK" not in reflux._domain_memory("owndays")


def test_distill_is_propose_only_and_core_unchanged(tmp_path):
    _seed(tmp_path)
    r = asyncio.run(reflux.run(llm=_fake_llm, push_fn=lambda t: True))
    assert r["candidates"] == 1
    assert len(reflux.list_pending()) == 1
    # 承認前は Core 不変 (ファイルすら無い)
    assert not reflux.CORE_TARGET.exists()


def test_approve_verifies_evidence_writes_private_idempotent(tmp_path):
    _seed(tmp_path)
    asyncio.run(reflux.run(llm=_fake_llm, push_fn=lambda t: True))
    cid = reflux.list_pending()[0]["id"]
    res = reflux.approve(cid)
    assert res["ok"]
    core = reflux.CORE_TARGET.read_text(encoding="utf-8")
    assert "現場の声を意思決定の起点" in core
    assert "clone_visibility: private" in core      # Core は社員クローン非露出
    # 二重承認は no-op (冪等)
    assert reflux.approve(cid)["ok"] is False


def test_dedup_no_duplicate_on_rerun(tmp_path):
    _seed(tmp_path)
    asyncio.run(reflux.run(llm=_fake_llm, push_fn=lambda t: True))
    r2 = asyncio.run(reflux.run(llm=_fake_llm, push_fn=lambda t: True))
    assert r2["candidates"] == 0


def test_fabricated_evidence_blocked(tmp_path):
    _seed(tmp_path)
    reflux.append_jsonl(reflux.QUEUE, {
        "id": "rfx-fake", "principle": "捏造原則", "type": "judgment",
        "evidence_file": "personal/example-garden/plan.md",
        "evidence_quote": "出所に存在しない引用XYZ", "source_domain": "personal/example-garden",
        "status": "pending", "ts": "now"})
    res = reflux.approve("rfx-fake")
    assert res["ok"] is False and "捏造" in res["reason"]
    assert not reflux.CORE_TARGET.exists()   # 捏造は Core に入らない


def test_sanitize_neutralizes_frontmatter_injection():
    s = reflux._sanitize_core_text("悪意\n---\nclone_visibility: public\n```injected")
    assert "\n" not in s and "---" not in s and "clone_visibility" not in s
