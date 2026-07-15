"""tests/smoke/test_drive_search_scoring.py — Drive 検索精度改善の契約 pin (★2026-07-13).

海山指摘「Drive検索の精度が悪い。GRPと単価を分けてワード検索するとか。よりLLM的な観点で」
(実例:「石川県のTVCMのGRP単価」で水戸/東海 PDCA が top 3)。守る不変条件:
- expand_query_structured が must (制約語) + keywords を構造化で返す (旧 list 揺れも受容)
- search_drive_semantic は「複数検索語ヒット + must ヒット」の決定論スコア順に並べる
  (旧: 検索実行順の先頭 30 件だけが rerank に渡り、関連 file が LLM に見えなかった)
- rerank へ evidence (中身ヒット語) と must_terms が渡る
- must 全滅時は result に must_hits=0 が載り、main.py が正直に明示する (source pin)
- Gemini 失敗 fallback: 新経路 = スコア順維持 / 旧直呼び = recency (後方互換)
"""
from __future__ import annotations

import asyncio
import json
from pathlib import Path

from services import gemini_query as gq

_ROOT = Path(__file__).resolve().parents[2]


def _run(coro):
    return asyncio.new_event_loop().run_until_complete(coro)


# ── expand_query_structured ──────────────────────────────
def test_structured_parses_dict_and_legacy_list(monkeypatch):
    async def fake_gen(prompt, response_json=False, max_tokens=512):
        return json.dumps({"must": ["石川"], "keywords": ["石川県", "TVCM", "GRP単価", "GRP"]})
    monkeypatch.setattr(gq, "_generate", fake_gen)
    st = _run(gq.expand_query_structured("石川県のTVCMのGRP単価は?"))
    assert st["must"] == ["石川"]
    assert "GRP" in st["keywords"] and "GRP単価" in st["keywords"]

    async def fake_gen_list(prompt, response_json=False, max_tokens=512):
        return json.dumps(["武蔵小山", "予算"])  # 旧形式揺れ
    monkeypatch.setattr(gq, "_generate", fake_gen_list)
    st = _run(gq.expand_query_structured("武蔵小山店の予算"))
    assert st["must"] == [] and st["keywords"] == ["武蔵小山", "予算"]


def test_expand_query_wrapper_merges_must_first(monkeypatch):
    async def fake_st(query, max_keywords=6):
        return {"must": ["石川"], "keywords": ["TVCM", "石川"]}
    monkeypatch.setattr(gq, "expand_query_structured", fake_st)
    kws = _run(gq.expand_query("q"))
    assert kws[0] == "石川" and kws.count("石川") == 1  # must 先頭 + dedup


# ── search_drive_semantic の決定論スコア ─────────────────
def _stub_gdrive(monkeypatch, mapping):
    """gdrive_sync は google package 依存 (MacBook 未 install) → stub module 注入で
    ローカルでも pipeline テストを走らせる (skip にしない = 決定論スコアは常時 pin)。"""
    import sys
    import types
    stub = types.ModuleType("gdrive_sync")
    stub.BOT_SEARCH_DEFAULT_MIMES = ["application/pdf"]
    stub.discover = (lambda q, folder, limit, mode, flag, since, mime,
                     content_check=True: mapping.get(q, []))
    stub.content_safe_filter = lambda files, max_workers=6: files  # 全通し (安全判定は別テスト)
    monkeypatch.setitem(sys.modules, "gdrive_sync", stub)


def test_scoring_puts_must_hit_first(monkeypatch):
    """「石川県」ヒットの file が、今日更新の generic 多数ヒット file より先頭に来る。"""
    ishikawa = {"id": "F1", "name": "石川地区 TVCM 出稿計画", "modifiedTime": "2026-01-01T00:00:00Z"}
    noise_a = {"id": "F2", "name": "水戸地区 PDCA", "modifiedTime": "2026-07-13T00:00:00Z"}
    noise_b = {"id": "F3", "name": "東海地区シフト", "modifiedTime": "2026-07-13T00:00:00Z"}
    mapping = {
        "石川県のTVCMのGRP単価は?": [noise_a],
        "石川県": [ishikawa],
        "TVCM": [ishikawa, noise_a, noise_b],
        "GRP": [noise_a],
    }

    async def fake_st(query, max_keywords=6):
        return {"must": ["石川"], "keywords": ["石川県", "TVCM", "GRP"]}

    captured = {}

    async def fake_rerank(query, files, top_n=3, evidence=None, must_terms=None):
        captured["files"] = files
        captured["evidence"] = evidence
        captured["must_terms"] = must_terms
        return files[:top_n]

    monkeypatch.setattr(gq, "expand_query_structured", fake_st)
    monkeypatch.setattr(gq, "rerank_results", fake_rerank)
    _stub_gdrive(monkeypatch, mapping)

    result = _run(gq.search_drive_semantic("石川県のTVCMのGRP単価は?", top_n=3))
    # must ヒット (石川) の F1 が、今日更新・複数ヒットの noise より先頭
    assert result["all"][0]["id"] == "F1"
    assert result["must_terms"] == ["石川"]
    assert result["must_hits"] == 1
    # rerank に evidence + must_terms が渡っている
    assert captured["must_terms"] == ["石川"]
    assert "石川県" in captured["evidence"]["F1"]
    # top にも F1 (rerank は入力順 mock なので先頭)
    assert result["top"][0]["id"] == "F1"


def test_must_miss_is_reported(monkeypatch):
    """制約語に 1 件もヒットしない場合 must_hits=0 が result に載る。"""
    noise = {"id": "N1", "name": "水戸地区 PDCA", "modifiedTime": "2026-07-13T00:00:00Z"}
    mapping = {"島根の売上": [noise], "GRP": [noise]}

    async def fake_st(query, max_keywords=6):
        return {"must": ["島根"], "keywords": ["GRP"]}

    async def fake_rerank(query, files, top_n=3, evidence=None, must_terms=None):
        return []

    monkeypatch.setattr(gq, "expand_query_structured", fake_st)
    monkeypatch.setattr(gq, "rerank_results", fake_rerank)
    _stub_gdrive(monkeypatch, mapping)

    result = _run(gq.search_drive_semantic("島根の売上", top_n=3))
    assert result["must_hits"] == 0 and result["must_terms"] == ["島根"]


# ── fallback の後方互換 ──────────────────────────────────
def test_rerank_fallback_split():
    files = [
        {"id": "a", "modifiedTime": "2026-01-01T00:00:00Z"},  # スコア順 1 位 (古い)
        {"id": "b", "modifiedTime": "2026-07-13T00:00:00Z"},  # スコア順 2 位 (新しい)
    ]
    # 新経路 (score_ordered): 入力順維持
    assert gq._rerank_fallback(files, 1, True)[0]["id"] == "a"
    # 旧直呼び: recency
    assert gq._rerank_fallback(files, 1, False)[0]["id"] == "b"


# ── main.py の must-miss 正直表示 (source pin) ───────────
def test_main_must_miss_warning_wired():
    src = (_ROOT / "main.py").read_text(encoding="utf-8")
    assert "must_hits" in src and "直接ヒットする file は無し" in src


def test_fallback_marks_degraded():
    """★2026-07-13: Gemini 失敗 fallback は rerank_confidence="degraded" を付けて返す
    (= 意味判定を経ていない機械選別、表示側が「参考」扱いに落とす)。"""
    files = [{"id": "a", "modifiedTime": "2026-01-01T00:00:00Z"},
             {"id": "b", "modifiedTime": "2026-07-13T00:00:00Z"}]
    out = gq._rerank_fallback(files, 2, True)
    assert all(f.get("rerank_confidence") == "degraded" for f in out)


def test_main_confidence_display_wired():
    """main.py が確度で表示を出し分ける (degraded / high 無し) source pin。"""
    src = (_ROOT / "main.py").read_text(encoding="utf-8")
    assert "機械選別 = 参考程度" in src
    assert "質問に直接答える資料は見つからなかった" in src
    assert "rerank_confidence" in src
