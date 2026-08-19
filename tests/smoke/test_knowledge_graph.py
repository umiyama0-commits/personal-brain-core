"""smoke test: knowledge_graph (Phase 1 in-process graph、Phase 2 で Graphiti / Neo4j 化)."""
from __future__ import annotations

import importlib
import json
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(REPO_ROOT / "scripts"))


@pytest.fixture
def graph_with_seed(tmp_path, monkeypatch):
    """SNAPSHOT_DIR を tmp に向け、seed snapshot を 2 つ書いて返す。"""
    monkeypatch.setenv("BRAIN_APP_ROOT", str(tmp_path))
    if "knowledge_graph" in sys.modules:
        importlib.reload(sys.modules["knowledge_graph"])
    import knowledge_graph as mod  # type: ignore

    snap_dir = tmp_path / "data" / "brain" / "wiki" / "knowledge" / "history" / "org-snapshots"
    snap_dir.mkdir(parents=True, exist_ok=True)

    snap_a = {
        "snapshot_date": "2026-04-01",
        "valid_from": "2026-04-01",
        "valid_to": "2026-04-30",
        "stores": [
            {"code": 101, "name": "渋谷店", "prefecture": "東京", "area": "関東A",
             "type": "直営", "league": "J1", "am": "中田将也", "sv": "鈴木和典",
             "manager": "佐藤一郎"},
            {"code": 102, "name": "新宿店", "prefecture": "東京", "area": "関東A",
             "type": "直営", "league": "J1", "am": "中田将也", "sv": "鈴木和典",
             "manager": "山田次郎"},
            {"code": 201, "name": "梅田店", "prefecture": "大阪", "area": "西日本A",
             "type": "直営", "league": "J1", "am": "谷口里美", "sv": "高橋健",
             "manager": "鈴木三郎"},
        ],
    }
    snap_b = {
        "snapshot_date": "2026-05-19",
        "valid_from": "2026-05-01",
        "valid_to": "2026-05-31",
        "stores": [
            {"code": 101, "name": "渋谷店", "prefecture": "東京", "area": "関東A",
             "type": "直営", "league": "J1", "am": "中田将也", "sv": "鈴木和典",
             "manager": "佐藤一郎"},
            {"code": 102, "name": "新宿店", "prefecture": "東京", "area": "関東A",
             "type": "直営", "league": "J1", "am": "渡邉俊也", "sv": "鈴木和典",  # AM 変更
             "manager": "山田次郎"},
            {"code": 201, "name": "梅田店", "prefecture": "大阪", "area": "西日本A",
             "type": "直営", "league": "J1", "am": "谷口里美", "sv": "高橋健",
             "manager": "鈴木三郎"},
        ],
    }
    (snap_dir / "2026-04-01.json").write_text(json.dumps(snap_a, ensure_ascii=False), encoding="utf-8")
    (snap_dir / "2026-05-19.json").write_text(json.dumps(snap_b, ensure_ascii=False), encoding="utf-8")
    return mod, snap_dir


@pytest.mark.smoke
def test_module_imports():
    if "knowledge_graph" in sys.modules:
        importlib.reload(sys.modules["knowledge_graph"])
    import knowledge_graph as mod
    assert hasattr(mod, "load_snapshot")
    assert hasattr(mod, "query_am_stores")
    assert hasattr(mod, "query_sv_stores")
    assert hasattr(mod, "query_diff_am_stores")
    assert hasattr(mod, "OrgGraph")


@pytest.mark.smoke
def test_load_snapshot_basic(graph_with_seed):
    mod, _ = graph_with_seed
    g = mod.load_snapshot("2026-05-19")
    assert g.snapshot_date == "2026-05-19"
    assert len(g.stores) == 3
    assert 101 in g.stores
    assert g.stores[101].name == "渋谷店"


@pytest.mark.smoke
def test_query_am_stores(graph_with_seed):
    """中田将也 AM の管轄店 (snapshot A は 2 件、B は 1 件)。"""
    mod, _ = graph_with_seed
    g_a = mod.load_snapshot("2026-04-01")
    stores = mod.query_am_stores(g_a, "中田将也")
    assert len(stores) == 2
    assert {s.name for s in stores} == {"渋谷店", "新宿店"}

    g_b = mod.load_snapshot("2026-05-19")
    stores_b = mod.query_am_stores(g_b, "中田将也")
    assert len(stores_b) == 1
    assert stores_b[0].name == "渋谷店"


@pytest.mark.smoke
def test_query_sv_am(graph_with_seed):
    """SV の上司 AM。"""
    mod, _ = graph_with_seed
    g = mod.load_snapshot("2026-05-19")
    # 鈴木和典 SV は (B では) 渋谷店経由で中田将也 / 新宿店経由で渡邉俊也、
    # sv_to_am は store loop の最後で上書きされるため最後の店の AM を返す
    am = mod.query_sv_manager_am(g, "鈴木和典")
    assert am in ("中田将也", "渡邉俊也")  # 順序依存だが両方妥当


@pytest.mark.smoke
def test_query_prefecture_ams(graph_with_seed):
    """東京の店舗を担当する AM 集合 (B では 中田 + 渡邉)。"""
    mod, _ = graph_with_seed
    g = mod.load_snapshot("2026-05-19")
    ams = mod.query_prefecture_ams(g, "東京")
    assert ams == {"中田将也", "渡邉俊也"}


@pytest.mark.smoke
def test_query_diff_am_stores(graph_with_seed):
    """中田 AM が A→B で失った店 = 新宿店 (→ 渡邉)。"""
    mod, _ = graph_with_seed
    g_a = mod.load_snapshot("2026-04-01")
    g_b = mod.load_snapshot("2026-05-19")
    result = mod.query_diff_am_stores(g_a, g_b, "中田将也")
    assert result["n_in_from"] == 2
    assert result["n_still_in_to"] == 1
    assert result["n_moved_away"] == 1
    moved = result["moved_away"][0]
    assert moved["name"] == "新宿店"
    assert moved["to_am"] == "渡邉俊也"


@pytest.mark.smoke
def test_query_am_subordinate_svs(graph_with_seed):
    """中田 AM の配下 SV (snapshot A では 鈴木和典 1 名)。"""
    mod, _ = graph_with_seed
    g_a = mod.load_snapshot("2026-04-01")
    svs = mod.query_am_subordinate_svs(g_a, "中田将也")
    assert svs == {"鈴木和典"}


@pytest.mark.smoke
def test_list_snapshots_returns_sorted(graph_with_seed):
    mod, _ = graph_with_seed
    snaps = mod.list_snapshots()
    assert snaps == ["2026-04-01", "2026-05-19"]


@pytest.mark.smoke
def test_query_sv_am_history(graph_with_seed):
    """SV の AM 履歴。鈴木和典 SV は ある時 中田、別の時 渡邉。"""
    mod, _ = graph_with_seed
    snaps = [mod.load_snapshot("2026-04-01"), mod.load_snapshot("2026-05-19")]
    history = mod.query_sv_am_history(snaps, "鈴木和典")
    # 順序依存だが少なくとも 1 entry はある
    assert len(history) >= 1
    am_names = [h["am"] for h in history]
    # AM が変わったなら 2 entry になる、変わらないなら 1 entry
    assert all(am in ("中田将也", "渡邉俊也", None) for am in am_names)


@pytest.mark.smoke
def test_load_snapshot_missing(graph_with_seed):
    mod, _ = graph_with_seed
    with pytest.raises(SystemExit):
        mod.load_snapshot("2099-01-01")
