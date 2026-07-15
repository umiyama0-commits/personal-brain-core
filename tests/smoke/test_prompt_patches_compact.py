"""smoke: prompt_patches_compact (★2026-07-05 prompt 監査 #11/#40/#41)

1. --gc-dead: dead bucket (intent_keywords / drive_search_patterns) を物理削除 + backup
2. propose は書き戻さない (propose-only)、canonical にも deny-filter 適用
3. --approve: 提案後に additions が変わっていたら abort (lost update 防止)
LLM は monkeypatch で偽装、network 非依存。
"""
from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "scripts"))

import prompt_patches_compact as ppc  # noqa: E402


@pytest.fixture()
def patches_env(monkeypatch, tmp_path):
    p = tmp_path / "system_prompt_patches.json"
    p.write_text(json.dumps({
        "system_prompt_additions": [f"ルール {i}: 何かの言い換え" for i in range(25)],
        "intent_keywords": {"a": ["a"]},
        "drive_search_patterns": ["x"],
    }, ensure_ascii=False), encoding="utf-8")
    monkeypatch.setattr(ppc, "PATCHES", p)
    monkeypatch.setattr(ppc, "PROPOSAL_DIR", tmp_path / "proposals")
    return p


@pytest.mark.smoke
def test_gc_dead_removes_buckets_with_backup(patches_env, tmp_path):
    r = ppc.gc_dead()
    assert r["ok"] and set(r["removed"]) == {"intent_keywords", "drive_search_patterns"}
    d = json.loads(patches_env.read_text(encoding="utf-8"))
    assert "intent_keywords" not in d and "drive_search_patterns" not in d
    assert d["system_prompt_additions"]                      # 有効 bucket は不変
    assert list(tmp_path.glob("*.bak-*")), "backup が無い"
    # 冪等
    assert ppc.gc_dead()["removed"] == []


@pytest.mark.smoke
def test_propose_is_propose_only_and_filters_denied(patches_env, monkeypatch, tmp_path):
    async def fake_llm(prompt, **kw):
        return json.dumps({
            "canonical": ["統合ルール A", "公式発表を参照して回答する"],   # 2 個目は deny 対象
            "mapping": {str(i): 0 for i in range(25)},
        })
    monkeypatch.setattr(ppc, "call_llm", fake_llm)
    before = patches_env.read_text(encoding="utf-8")
    r = asyncio.run(ppc.propose())
    assert r["ok"] and r["proposal"]
    assert patches_env.read_text(encoding="utf-8") == before, "propose が書き戻した (propose-only 違反)"
    prop = json.loads((tmp_path / "proposals" / r["proposal"]).read_text(encoding="utf-8"))
    assert prop["status"] == "pending"
    assert prop["canonical"] == ["統合ルール A"], "deny 対象が canonical に残った"


@pytest.mark.smoke
def test_approve_applies_and_aborts_on_concurrent_change(patches_env, monkeypatch, tmp_path):
    async def fake_llm(prompt, **kw):
        return json.dumps({"canonical": ["統合ルール A"],
                           "mapping": {str(i): 0 for i in range(25)}})
    monkeypatch.setattr(ppc, "call_llm", fake_llm)
    pid = asyncio.run(ppc.propose())["proposal"]

    # 夜間 append を模擬 → sha 不一致 → abort
    d = json.loads(patches_env.read_text(encoding="utf-8"))
    d["system_prompt_additions"].append("夜間に増えた新ルール")
    patches_env.write_text(json.dumps(d, ensure_ascii=False), encoding="utf-8")
    r = ppc.approve(pid)
    assert not r["ok"] and "変更" in r["error"]

    # 元に戻せば適用できる
    d["system_prompt_additions"].pop()
    patches_env.write_text(json.dumps(d, ensure_ascii=False), encoding="utf-8")
    r2 = ppc.approve(pid)
    assert r2["ok"] and r2["applied"] == 1
    saved = json.loads(patches_env.read_text(encoding="utf-8"))["system_prompt_additions"]
    assert saved == ["統合ルール A"]
    # 二重承認は不可 (status=applied)
    assert not ppc.approve(pid)["ok"]


@pytest.mark.smoke
def test_propose_mapping_type_coercion_and_denied_canonical_shift(patches_env, monkeypatch, tmp_path):
    """★Reviewer MAJOR-1/2 の regression pin。

    - mapping 値の型ゆれ ("0" 文字列 / garbage) → digit 文字列は int に coerce、
      不明値は retained (原文残置、黙って消さない)
    - denied canonical 宛の entry は「統合済み」扱いにせず retained に回す
      (filter 後 index で判定すると誤った canonical に吸収されて黙って消える)
    """
    async def fake_llm(prompt, **kw):
        return json.dumps({
            "canonical": ["政府の公式発表を参照して回答する",   # index 0 = deny 対象
                          "統合ルール B"],                        # index 1 = 正当
            "mapping": {
                # entry 0-9 → denied canonical 0 (文字列 index)、10-19 → canonical 1 (文字列)
                # 20 → garbage、21-24 → "retained"
                **{str(i): "0" for i in range(10)},
                **{str(i): "1" for i in range(10, 20)},
                "20": "unknown",
                **{str(i): "retained" for i in range(21, 25)},
            },
        })
    monkeypatch.setattr(ppc, "call_llm", fake_llm)
    r = asyncio.run(ppc.propose())
    prop = json.loads((tmp_path / "proposals" / r["proposal"]).read_text(encoding="utf-8"))
    assert prop["canonical"] == ["統合ルール B"], "denied canonical が残った"
    # denied canonical 宛の 10 件 + garbage 1 件 + retained 明示 4 件 = 15 件が原文残置
    assert len(prop["retained"]) == 15, prop["retained"]
    # 正当 canonical 宛の 10 件 (文字列 index "1") は統合済み = retained に居ない
    assert prop["after_count"] == 1 + 15
