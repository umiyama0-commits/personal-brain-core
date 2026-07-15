"""smoke: self_improve の patches 蓄積機構 (★2026-07-05 prompt 監査 A1/B)

1. 正規化 dedup: 空白・句読点ゆれの同義文を append しない (旧 exact-match は素通り)
2. 上限 cap: MAX_PROMPT_ADDITIONS 超過で最古 drop (黙って消さず applied に記録)
3. deny-filter: 「公式発表を参照して回答」「業界平均から推定値を提供」系 = 捏造招待を reject
LLM/network 非依存。
"""
from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

import self_improve  # noqa: E402


def _run(monkeypatch, tmp_path, improvements, existing=None):
    p = tmp_path / "patches.json"
    if existing is not None:
        p.write_text(json.dumps({"system_prompt_additions": existing}, ensure_ascii=False),
                     encoding="utf-8")
    monkeypatch.setattr(self_improve, "SYSTEM_PROMPT_OVERRIDES", p)
    applied = asyncio.run(self_improve.apply_improvements(improvements))
    saved = json.loads(p.read_text(encoding="utf-8"))["system_prompt_additions"] if p.exists() else []
    return applied, saved


@pytest.mark.smoke
def test_normalized_dedup_rejects_paraphrase_whitespace(monkeypatch, tmp_path):
    existing = ["繰り返し質問には過去の回答を踏まえて答える。"]
    imps = [
        # 空白・末尾句読点だけ違う「実質同文」
        {"type": "system_prompt_addition",
         "content": "繰り返し質問には 過去の回答を踏まえて答える"},
        {"type": "system_prompt_addition", "content": "新しいルール: 曖昧な質問は一言確認する。"},
    ]
    applied, saved = _run(monkeypatch, tmp_path, imps, existing)
    assert len(saved) == 2, saved  # 同義文は増えず、新規 1 件だけ追加
    assert any("曖昧な質問" in s for s in saved)


@pytest.mark.smoke
def test_cap_drops_oldest_and_reports(monkeypatch, tmp_path):
    monkeypatch.setattr(self_improve, "MAX_PROMPT_ADDITIONS", 5)
    existing = [f"既存ルール {i} 番" for i in range(5)]
    imps = [{"type": "system_prompt_addition", "content": "一番新しいルール"}]
    applied, saved = _run(monkeypatch, tmp_path, imps, existing)
    assert len(saved) == 5
    assert saved[-1] == "一番新しいルール"
    assert "既存ルール 0 番" not in saved          # 最古が落ちる
    assert any("drop" in a for a in applied)       # 黙って消えない


@pytest.mark.smoke
def test_deny_filter_rejects_fabrication_invitations(monkeypatch, tmp_path):
    imps = [
        {"type": "system_prompt_addition",
         "content": "日本の予算に関する質問は政府の公式発表や信頼できる経済データベースを参照して回答する。"},
        {"type": "system_prompt_addition",
         "content": "データが無い時は業界平均や成長率から推定値を提供する。"},
        {"type": "system_prompt_addition", "content": "曖昧な単位は一言確認してから答える。"},
    ]
    applied, saved = _run(monkeypatch, tmp_path, imps)
    assert len(saved) == 1, saved                  # 捏造招待 2 件は reject
    assert "曖昧な単位" in saved[0]


@pytest.mark.smoke
def test_addition_denied_unit():
    # deny: 確実な肯定形の捏造招待のみ (実 patches.json の混入 2 件と同型)
    assert self_improve._addition_denied("政府の公式発表を参照して回答する")
    assert self_improve._addition_denied("業界平均などから推定値を提供")
    assert self_improve._addition_denied("web検索で最新情報を参照する")
    # allow: 内部ソース参照・否定形・禁止形の正当ルール (★DA cross-check の誤爆 5 例を pin)
    assert not self_improve._addition_denied("wiki にある範囲の数字だけ答える")
    assert not self_improve._addition_denied(
        "Monday Dash の数値は knowledge/owndays-monday-dash-latest.md を参照して回答する")
    assert not self_improve._addition_denied("店舗売上は owndays-daily-stores.md を参照して回答すること")
    assert not self_improve._addition_denied("社内データベースを参照できない場合は正直に言う")
    assert not self_improve._addition_denied("過去の会話履歴を参照して回答の一貫性を保つ")
    assert not self_improve._addition_denied("ウェブサイトのURLは検索せず提供されたリンクのみ使う")
