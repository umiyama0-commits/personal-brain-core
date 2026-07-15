"""edge_store (Phase 1 sidecar) + bridge_proposer 純関数のテスト (2026-07-05 ADR §3)。"""
import json
import pathlib
import sys

import pytest

REPO = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))
sys.path.insert(0, str(REPO / "scripts"))

from brain_wiki_helpers.edge_store import (  # noqa: E402
    append_proposals, decide, list_pending, load_approved_edges,
    pair_key, proposal_id, validate_pair,
)


def _wiki(tmp_path, *rels):
    wd = tmp_path / "wiki"
    for r in rels:
        p = wd / r
        p.parent.mkdir(parents=True, exist_ok=True)
        # ★2026-07-11 clone_visibility: public 明記 — build_graph_data の token tier (admin=False)
        #   は非 public を fail-safe 除外するため (validate_pair 系は path 判定のみで無影響)。
        p.write_text(
            f"---\nclone_visibility: public\n---\n# {pathlib.Path(r).stem}\n本文",
            encoding="utf-8")
    return wd


# ─── validate / id ───

def test_validate_pair(tmp_path):
    wd = _wiki(tmp_path, "judgment/a.md", "knowledge/b.md", "personal/pj/x.md")
    assert validate_pair("judgment/a.md", "knowledge/b.md", wd) is None
    assert validate_pair("judgment/a.md", "judgment/a.md", wd) is not None      # 自己ループ
    assert validate_pair("judgment/a.md", "ghost.md", wd) is not None           # 実在しない
    assert validate_pair("judgment/a.md", "personal/pj/x.md", wd) is not None   # 深層 private (§1.17)
    assert validate_pair("interview/deep.md", "knowledge/b.md", wd) is not None
    assert validate_pair("", "knowledge/b.md", wd) is not None


def test_proposal_id_symmetric():
    assert proposal_id("a.md", "b.md", "related") == proposal_id("b.md", "a.md", "related")
    assert pair_key("b", "a") == ("a", "b")


def test_validate_pair_traversal_blocked(tmp_path):
    """`..`/絶対 path での封じ込め迂回を遮断 (code-review privacy-1)。"""
    wd = _wiki(tmp_path, "knowledge/b.md")
    # wiki の外側に「深層 private 相当」の実ファイルを置く
    outside = tmp_path / "wiki2" / "personal" / "pj" / "x.md"
    outside.parent.mkdir(parents=True)
    outside.write_text("秘", encoding="utf-8")
    evil = "../wiki2/personal/pj/x.md"
    assert validate_pair(evil, "knowledge/b.md", wd) is not None
    assert validate_pair("/etc/hosts", "knowledge/b.md", wd) is not None
    # wiki 内の deep-private への traversal 表記も不可
    inner = _wiki(tmp_path, "personal/pj/y.md")  # 同じ wd に追加
    assert validate_pair("knowledge/../personal/pj/y.md", "knowledge/b.md", wd) is not None


def test_reopen_undo(tmp_path):
    from brain_wiki_helpers.edge_store import reopen
    wd = _wiki(tmp_path, "judgment/a.md", "knowledge/b.md")
    append_proposals(tmp_path, wd, [
        {"from": "judgment/a.md", "to": "knowledge/b.md", "source": "embed", "score": 1}])
    pid = list_pending(tmp_path)[0]["id"]
    decide(tmp_path, [pid], approve=False)
    assert list_pending(tmp_path) == []
    r = reopen(tmp_path, [pid, "br-ghost"])
    assert r["done"] == [pid] and "br-ghost" in r["skipped"]
    assert [p["id"] for p in list_pending(tmp_path)] == [pid]


# ─── queue の一生: append → dedup → decide → edges ───

def test_queue_lifecycle(tmp_path):
    wd = _wiki(tmp_path, "judgment/a.md", "knowledge/b.md", "analysis/c.md")
    br = tmp_path  # brain_root (graph/ が下にできる)

    n = append_proposals(br, wd, [
        {"from": "judgment/a.md", "to": "knowledge/b.md", "source": "embed",
         "why": "embedding 類似 0.85", "score": 0.85},
        {"from": "knowledge/b.md", "to": "judgment/a.md", "source": "cooc"},   # 同 pair 逆順 → dedup
        {"from": "analysis/c.md", "to": "knowledge/b.md", "relation": "evidence_for",
         "source": "compile", "score": 0.5},
        {"from": "analysis/c.md", "to": "ghost.md", "source": "embed"},        # 実在しない → 落ちる
    ])
    assert n == 2
    pend = list_pending(br)
    assert len(pend) == 2
    assert pend[0]["score"] >= pend[1]["score"]                                # score 降順
    assert all(r["relation"] in ("related", "evidence_for") for r in pend)

    # 再 append は dedup で 0
    assert append_proposals(br, wd, [
        {"from": "judgment/a.md", "to": "knowledge/b.md", "source": "embed"}]) == 0

    # 個別承認 + 却下
    ids = [r["id"] for r in pend]
    r = decide(br, [ids[0]], approve=True)
    assert r["done"] == [ids[0]]
    r = decide(br, [ids[1], "br-ghost"], approve=False)
    assert ids[1] in r["done"] and "br-ghost" in r["skipped"]
    assert list_pending(br) == []                                              # 全て決定済

    edges = load_approved_edges(br)
    assert len(edges) == 1 and edges[0]["id"] == ids[0]


def test_decide_all_batch(tmp_path):
    wd = _wiki(tmp_path, "judgment/a.md", "knowledge/b.md", "analysis/c.md")
    append_proposals(tmp_path, wd, [
        {"from": "judgment/a.md", "to": "knowledge/b.md", "source": "embed", "score": 1},
        {"from": "analysis/c.md", "to": "knowledge/b.md", "source": "cooc", "score": 2},
    ])
    r = decide(tmp_path, ["all"], approve=True)
    assert len(r["done"]) == 2 and list_pending(tmp_path) == []
    assert len(load_approved_edges(tmp_path)) == 2


def test_queue_relation_normalized_and_why_capped(tmp_path):
    wd = _wiki(tmp_path, "judgment/a.md", "knowledge/b.md")
    append_proposals(tmp_path, wd, [
        {"from": "judgment/a.md", "to": "knowledge/b.md", "source": "compile",
         "relation": "この価値観が影響した (自由文)", "why": "x" * 500}])
    p = list_pending(tmp_path)[0]
    assert p["relation"] == "related"          # 自由文 relation → 閉語彙へ (捏造型付け抑制)
    assert len(p["why"]) <= 120                # 本文流入の cap (§1.17)


# ─── brain_graph への描画 ───

def test_bridge_edge_rendered_in_graph(tmp_path):
    from brain_graph import build_graph_data
    wd = _wiki(tmp_path, "judgment/a.md", "knowledge/b.md")
    (wd / "judgment/a.md").write_text(
        "---\nclone_visibility: public\nupdated: 2026-07-01\n---\n# a\n" + "本文" * 80, encoding="utf-8")
    (wd / "knowledge/b.md").write_text(
        "---\nclone_visibility: public\nupdated: 2026-07-01\n---\n# b\n" + "本文" * 80, encoding="utf-8")
    append_proposals(tmp_path, wd, [
        {"from": "judgment/a.md", "to": "knowledge/b.md", "source": "embed", "score": 0.9}])
    decide(tmp_path, ["all"], approve=True)

    data = build_graph_data(wd, show_all=True)
    bridges = [e for e in data["edges"] if e.get("type") == "bridge"]
    assert len(bridges) == 1
    assert data["stats"]["bridge_edges"] == 1
    # 被リンクとして in_degree にも効く (孤島が graph 上で繋がる)
    by_id = {n["id"]: n for n in data["nodes"]}
    assert by_id["judgment/a.md"]["in_degree"] + by_id["knowledge/b.md"]["in_degree"] >= 1

    # ★承認済み bridge の両端は surface 強制 (承認したのに見えない実測 17/32 の対策):
    #   低 score でも storage に沈まず、エッジは必ず描画される
    for i in range(12):   # 他ノードを足して surface cutoff を厳しくする
        p = wd / f"knowledge/pad{i}.md"
        p.write_text(f"---\nclone_visibility: public\nupdated: 2026-07-05\n---\n# pad{i}\n" + "厚" * 500, encoding="utf-8")
    data2 = build_graph_data(wd, surface_pct=5, show_all=False)
    assert data2["stats"]["bridge_edges"] == 1
    surf_ids = {n["id"] for n in data2["nodes"] if not n.get("is_storage")}
    assert {"judgment/a.md", "knowledge/b.md"} <= surf_ids


def test_bridge_edge_to_missing_node_dropped(tmp_path):
    """edges.jsonl に古い/深層 private の endpoint が居ても graph は落ちず、エッジも出ない。"""
    from brain_graph import build_graph_data
    wd = _wiki(tmp_path, "knowledge/b.md")
    gd = tmp_path / "graph"
    gd.mkdir(parents=True, exist_ok=True)
    (gd / "edges.jsonl").write_text(json.dumps({
        "id": "br-x", "from": "interview/deep.md", "to": "knowledge/b.md",
        "relation": "related", "source": "embed", "approved_at": "2026-07-05"}) + "\n",
        encoding="utf-8")
    data = build_graph_data(wd, show_all=True)
    assert data["stats"]["bridge_edges"] == 0


# ─── bridge_proposer 純関数 ───

def test_proposer_scan_and_orphans(tmp_path):
    from bridge_proposer import find_orphans, scan_wiki
    wd = _wiki(tmp_path, "judgment/axes.md", "analysis/w.md",
               "knowledge/hub.md", "personal/pj/secret.md", "index.md")
    (wd / "knowledge/hub.md").write_text(
        "# hub\n[[analysis/w]] を参照", encoding="utf-8")
    # 本番 root index.md は全ページにリンクする (1,540 links 実測) — カタログ掲載は接続でない
    (wd / "index.md").write_text(
        "# index\n[[judgment/axes]] [[analysis/w]] [[knowledge/hub]]", encoding="utf-8")
    scan = scan_wiki(wd)
    assert "personal/pj/secret.md" not in scan["files"]            # §1.17 walk 除外
    orphans = find_orphans(scan)
    assert "judgment/axes.md" in orphans                           # index からのみ被リンク = 孤島
    assert "analysis/w.md" not in orphans                          # hub (非 index) から被リンク
    assert pair_key("knowledge/hub.md", "analysis/w.md") in scan["linked"]
    assert pair_key("index.md", "judgment/axes.md") in scan["linked"]  # dedup 用には残る


def test_handle_command_guards(tmp_path, monkeypatch):
    """ng all の確認要求 / all+個別の混在エラー / prefix 解決 (code-review UX-2/4/5)。"""
    import bridge_proposer as bp
    wd = _wiki(tmp_path, "judgment/a.md", "knowledge/b.md")
    monkeypatch.setattr(bp, "BRAIN_ROOT", tmp_path)
    append_proposals(tmp_path, wd, [
        {"from": "judgment/a.md", "to": "knowledge/b.md", "source": "embed", "score": 1}])
    pid = list_pending(tmp_path)[0]["id"]

    assert "ng all!" in bp.handle_command("ng all")            # 確認要求 (実行されない)
    assert list_pending(tmp_path) != []
    assert "同時指定" in bp.handle_command(f"ok all {pid}")     # 混在は明示エラー
    assert list_pending(tmp_path) != []
    r = bp.handle_command(f"ok {pid[3:7]}")                    # prefix (br- 省略 4字) 解決
    assert "1 件" in r and list_pending(tmp_path) == []


def test_render_pending_readable(tmp_path, monkeypatch):
    """承認 UI はタイトル+分類+平文根拠で読める (2026-07-05 海山 feedback)。"""
    import bridge_proposer as bp
    wd = _wiki(tmp_path, "judgment/a.md", "knowledge/b.md")
    (wd / "judgment/a.md").write_text("---\nx: 1\n---\n# 判断のまとめ 5月\n本文", encoding="utf-8")
    (wd / "knowledge/b.md").write_text("# OWNDAYS 店舗史\n本文", encoding="utf-8")
    monkeypatch.setattr(bp, "BRAIN_ROOT", tmp_path)
    monkeypatch.setattr(bp, "WIKI_DIR", wd)
    append_proposals(tmp_path, wd, [
        {"from": "judgment/a.md", "to": "knowledge/b.md", "source": "cooc", "score": 38}])
    out = bp.handle_command("")
    assert "「判断のまとめ 5月」(判断軸)" in out
    assert "「OWNDAYS 店舗史」(知識)" in out
    assert "実際の会話で 38回 一緒に想起された" in out
    assert ".md" not in out.split("承認:")[0]          # 生 path を見せない
    # 4字 id で承認できる案内と実動作
    sid = list_pending(tmp_path)[0]["id"][3:7]
    assert f"[{sid}]" in out
    assert "1 件" in bp.handle_command(f"ok {sid}")


def test_proposer_embed_proposals_pure():
    from bridge_proposer import embed_proposals
    scan = {"linked": set()}
    vecs = {
        "judgment/o.md": [1.0, 0.0],
        "knowledge/near.md": [0.99, 0.14],   # cos ≈ 0.990
        "knowledge/far.md": [0.0, 1.0],      # cos = 0
    }
    out = embed_proposals(scan, ["judgment/o.md"], vecs)
    assert len(out) == 1
    assert out[0]["to"] == "knowledge/near.md" and out[0]["source"] == "embed"
