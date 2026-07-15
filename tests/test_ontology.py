"""記憶層オントロジー Phase 0 (2026-07-05 ADR wiki-ontology-multilayer) のテスト。

ontology.py 純関数 + brain_graph.build_graph_data への配線 (layer/color_layer/減衰) を検証。
書込ゼロ設計なので fixture は tmp_path のみ、本物 data/ には触らない。
"""
import pathlib
import sys

import pytest

REPO = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))

from brain_wiki_helpers.ontology import (  # noqa: E402
    LAYER_COLORS, LAYER_LABELS, LAYERS,
    LAYER_CORE, LAYER_EPISODIC, LAYER_PROCEDURAL, LAYER_SEMANTIC,
    layer_of, node_kind_of,
)


# ─── 純関数: layer_of ───

@pytest.mark.parametrize("rel,expected", [
    ("identity.md", LAYER_CORE),
    ("style.md", LAYER_CORE),
    ("thinking.md", LAYER_CORE),
    ("style/response-bank.md", LAYER_PROCEDURAL),
    ("judgment/axes.md", LAYER_PROCEDURAL),
    ("knowledge/owndays-vmv.md", LAYER_SEMANTIC),
    ("analysis/ai-trends-owndays.md", LAYER_SEMANTIC),
    ("hobbies/manga/3-gatsu-no-lion.md", LAYER_SEMANTIC),
    ("decisions/2026-05-12-jcs.md", LAYER_SEMANTIC),
    ("people/someone.md", LAYER_SEMANTIC),
    ("meetings/2026-07-01-mtg.md", LAYER_EPISODIC),
    ("sales/2026-07/daily.md", LAYER_EPISODIC),
    ("interview/chronicle.md", LAYER_EPISODIC),
    ("personal/example-garden/plan.md", LAYER_EPISODIC),
])
def test_layer_of_mapping(rel, expected):
    assert layer_of(rel) == expected


def test_layer_of_is_total():
    """どんな入力でも例外なく既知の層を返す (total function)。"""
    for weird in ("", "unknown-dir/x.md", "index.md", "a/b/c/d.md", None, 123):
        assert layer_of(weird) in LAYERS


def test_layer_of_case_insensitive():
    """APFS の case-variant path でも誤分類しない (code-review 2026-07-05 A-3)。"""
    assert layer_of("Meetings/2026-07-01.md") == LAYER_EPISODIC
    assert layer_of("Identity.md") == LAYER_CORE
    assert node_kind_of("Decisions/2026-x.md") == "decision"


def test_core_root_files_match_domain():
    """人格核の定義は domain.CORE_FILES が単一真実源 (drift 防止、review reuse-2)。"""
    from brain_wiki_helpers.domain import CORE_FILES
    for f in CORE_FILES:
        assert layer_of(f) == LAYER_CORE


def test_layer_root_unknown_file_is_semantic():
    # root 直下でも CORE files 以外は core に昇格しない
    assert layer_of("index.md") == LAYER_SEMANTIC


def test_layer_palette_complete():
    """全層に配色とラベルがある (graph 側の KeyError を構造的に防ぐ)。"""
    for L in LAYERS:
        assert L in LAYER_COLORS and LAYER_COLORS[L]["bg"].startswith("#")
        assert L in LAYER_LABELS


# ─── 純関数: node_kind_of ───

@pytest.mark.parametrize("rel,expected", [
    ("hobbies/index.md", "index"),
    ("hobbies/books/index.md", "index"),
    ("decisions/2026-05-12-jcs.md", "decision"),
    ("analysis/whitespace.md", "analysis"),
    ("knowledge/owndays-vmv.md", "note"),
    ("style.md", "note"),
])
def test_node_kind_of(rel, expected):
    assert node_kind_of(rel) == expected


# ─── build_graph_data への配線 ───

def _mk(root: pathlib.Path, rel: str, body: str = "本文" * 60, links: str = ""):
    p = root / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(
        # ★2026-07-11 clone_visibility: public 明記 — build_graph_data の token tier (admin=False)
        #   は clone_visibility: private / 未設定を fail-safe 除外するため、構造テストの
        #   通常ノードは public を明示する (深層 private path は下でも別途 path で除外される)。
        f"---\nclone_visibility: public\nupdated: 2026-07-01\ntags: [t1]\n---\n"
        f"# {pathlib.Path(rel).stem}\n{body}\n{links}\n",
        encoding="utf-8",
    )


def test_build_graph_data_layers(tmp_path):
    from brain_graph import build_graph_data

    _mk(tmp_path, "knowledge/a.md", links="[[hobbies/index]]")
    _mk(tmp_path, "style/b.md")
    _mk(tmp_path, "meetings/c.md")
    _mk(tmp_path, "hobbies/index.md", body="い" * 9000)  # 巨大 index (減衰対象)
    _mk(tmp_path, "personal/pj/secret.md")               # 深層 private (除外されるべき)
    _mk(tmp_path, "interview/deep.md")                   # 同上 (★2026-07-03 v3)

    data = build_graph_data(tmp_path, show_all=True)
    by_id = {n["id"]: n for n in data["nodes"]}

    # 深層 private は node に出ない (§1.17)
    assert not any(i.startswith(("personal/", "interview/")) for i in by_id)

    # 全 surface node に layer + 両配色が付く
    for n in by_id.values():
        assert n["layer"] in LAYERS
        assert "color_layer" in n and "background" in n["color_layer"]
    assert by_id["knowledge/a.md"]["layer"] == LAYER_SEMANTIC
    assert by_id["style/b.md"]["layer"] == LAYER_PROCEDURAL
    assert by_id["meetings/c.md"]["layer"] == LAYER_EPISODIC

    # index は視覚減衰 (norm 段階で cap ≒ value 29、font も追従)
    assert by_id["hobbies/index.md"]["kind"] == "index"
    assert by_id["hobbies/index.md"]["value"] <= 30

    # ★index の外れ値 score が正規化レンジを支配しない (review altitude-1):
    #   非 index ノードの value が index に潰されて全て下限に張り付かないこと
    non_index_values = [n["value"] for i, n in by_id.items() if n["kind"] != "index"]
    assert max(non_index_values) >= 22, "非 index が index 外れ値に圧縮されている"

    # 凡例メタ (count 込み) + 層別 stats
    assert [m["key"] for m in data["layer_meta"]] == list(LAYERS)
    assert all("count" in m for m in data["layer_meta"])
    layers = data["stats"]["layers"]
    assert layers[LAYER_EPISODIC] == 1 and layers[LAYER_PROCEDURAL] == 1
    assert sum(layers.values()) == data["stats"]["node_count"]


def test_storage_hub_has_layer_border(tmp_path):
    """storage ハブも記憶層の縁色を持つ (layer モードで凡例と矛盾しない、review B-1)。"""
    from brain_graph import build_graph_data

    for i in range(6):   # knowledge を 6 枚 → MIN_PER_GROUP(3) 超の分は storage へ
        _mk(tmp_path, f"knowledge/k{i}.md")
    data = build_graph_data(tmp_path, surface_pct=5, show_all=False)
    hubs = [n for n in data["nodes"] if n.get("is_storage")]
    assert hubs, "storage ハブが生成されていない (fixture 前提崩れ)"
    for h in hubs:
        assert h["layer"] in LAYERS
        assert h["color_layer"]["border"] == LAYER_COLORS[h["layer"]]["bg"]
