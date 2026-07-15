"""connectome.py の隔離テスト(index 不要・決定論的・純粋関数)。"""
import pathlib
import sys

import pytest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

from scripts.connectome import (  # noqa: E402
    build_cooccurrence_graph,
    hebbian_update,
    merge_graphs,
    spreading_activation,
)


# --- build_cooccurrence_graph ---------------------------------------------

def test_cooccurrence_basic_symmetric():
    g = build_cooccurrence_graph([{"doc_ids": ["a", "b", "c"], "quality": 1.0}])
    assert g["a"]["b"] == 1.0 and g["a"]["c"] == 1.0 and g["b"]["c"] == 1.0
    assert g["b"]["a"] == 1.0  # 対称


def test_cooccurrence_quality_accumulates_and_dedup():
    events = [
        {"doc_ids": ["a", "b"], "quality": 0.5},
        {"doc_ids": ["a", "b", "b"], "quality": 0.5},  # 重複 b は 1 回に畳む
    ]
    g = build_cooccurrence_graph(events)
    assert g["a"]["b"] == pytest.approx(1.0)
    assert "b" not in g.get("b", {})  # 自己ループなし


def test_cooccurrence_min_quality_filter():
    g = build_cooccurrence_graph([{"doc_ids": ["a", "b"], "quality": 0.2}], min_quality=0.5)
    assert g == {}


def test_cooccurrence_asymmetric_option():
    g = build_cooccurrence_graph([{"doc_ids": ["a", "b"]}], symmetric=False)
    assert g["a"]["b"] == 1.0
    assert "b" not in g  # 逆向きエッジを張らない


# --- spreading_activation --------------------------------------------------

def test_spreading_one_hop_excludes_seeds():
    g = {"a": {"b": 1.0, "c": 0.5}}
    out = dict(spreading_activation(g, ["a"], hops=1, decay=1.0))
    assert "a" not in out
    assert out["b"] == pytest.approx(1.0)
    assert out["c"] == pytest.approx(0.5)


def test_spreading_two_hop_decay_compounds():
    g = {"a": {"b": 1.0}, "b": {"c": 1.0}}
    out = dict(spreading_activation(g, ["a"], hops=2, decay=0.5))
    assert out["b"] == pytest.approx(0.5)      # 1*1*0.5^1
    assert out["c"] == pytest.approx(0.125)    # 0.5(frontier)*1*0.5^2


def test_spreading_multipath_sums():
    # a→c と b→c の 2 経路。seed 2 個 → c は両方から活性を受ける
    g = {"a": {"c": 1.0}, "b": {"c": 1.0}}
    out = dict(spreading_activation(g, ["a", "b"], hops=1, decay=1.0))
    assert out["c"] == pytest.approx(2.0)


def test_spreading_top_n_and_min_activation():
    g = {"a": {"b": 0.9, "c": 0.4, "d": 0.05}}
    out = spreading_activation(g, ["a"], hops=1, decay=1.0, top_n=2)
    assert [n for n, _ in out] == ["b", "c"]  # 降順 top2
    out2 = spreading_activation(g, ["a"], hops=1, decay=1.0, min_activation=0.1)
    assert "d" not in dict(out2)  # 0.05 < 0.1 で除外


def test_spreading_no_edges_returns_empty():
    assert spreading_activation({}, ["x"], hops=2) == []


# --- merge_graphs ----------------------------------------------------------

def test_merge_weighted():
    g1 = {"a": {"b": 1.0}}
    g2 = {"a": {"b": 2.0, "c": 1.0}}
    m = merge_graphs(g1, g2, weights=[1.0, 0.5])
    assert m["a"]["b"] == pytest.approx(2.0)  # 1.0 + 2.0*0.5
    assert m["a"]["c"] == pytest.approx(0.5)


def test_merge_weight_length_mismatch_raises():
    with pytest.raises(ValueError):
        merge_graphs({"a": {"b": 1.0}}, weights=[1.0, 1.0])


# --- hebbian_update --------------------------------------------------------

def test_hebbian_potentiate_decay_keep():
    g = {"a": {"b": 1.0}}
    g2 = hebbian_update(
        g, [{"doc_ids": ["a", "c"], "quality": 1.0}],
        lr=0.1, decay=0.01, prune_below=1e-3,
    )
    assert g2["a"]["b"] == pytest.approx(0.99)   # 1.0*(1-0.01) 減衰
    assert g2["a"]["c"] == pytest.approx(0.1)    # 新規強化 lr*q
    assert g2["c"]["a"] == pytest.approx(0.1)    # 対称


def test_hebbian_prunes_weak_edges_and_isolated_nodes():
    g = {"a": {"b": 1e-4}}  # 既に弱い
    g2 = hebbian_update(g, [], decay=0.01, prune_below=1e-3)
    assert g2 == {}  # 減衰 → 閾値未満 → 刈り込み → 孤立ノード除去
