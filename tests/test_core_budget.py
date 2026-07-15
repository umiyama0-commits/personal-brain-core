"""tests/test_core_budget.py — ★2026-07-03 P3b core 全体予算スケーラの回帰テスト。

背景: core 常駐 118K (general) > gate 90K で vector が本番 100% skip していた
(世界水準評価 2026-07-02 の最低次元 C+ の根治レバー)。
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from brain_wiki_helpers.core_budget import scale_core_targets  # noqa: E402


def test_no_scaling_when_within_budget():
    proj = [("a.md", 5000), ("b.md", 3000)]
    assert scale_core_targets(proj, 10_000) == {}


def test_no_scaling_when_budget_disabled_or_negative():
    proj = [("a.md", 5000)]
    assert scale_core_targets(proj, 0) == {}
    assert scale_core_targets(proj, -100) == {}


def test_proportional_scaling_preserves_ratio():
    # 合計 30K → 予算 15K = scale 0.5
    proj = [("big.md", 20_000), ("small.md", 10_000)]
    out = scale_core_targets(proj, 15_000)
    assert out["big.md"] == 10_000
    assert out["small.md"] == 5_000
    # priority 比 (2:1) が縮小後も保存される
    assert out["big.md"] / out["small.md"] == 2.0


def test_floor_protects_small_files():
    proj = [("big.md", 50_000), ("tiny.md", 1_000)]
    out = scale_core_targets(proj, 10_000)  # scale ≈ 0.196 → tiny は 196 → floor 800
    assert out["tiny.md"] == 800
    assert out["big.md"] == int(50_000 * (10_000 / 51_000))


def test_scaled_total_close_to_budget():
    proj = [(f"f{i}.md", 6_000) for i in range(20)]  # 120K (実測 core に近い形)
    budget = 70_000
    out = scale_core_targets(proj, budget)
    total = sum(out.values())
    # floor 発動なしのケースでは予算 ±1% に収まる (int 切捨てで下振れ)
    assert total <= budget
    assert total >= budget * 0.98


def test_all_floor_edge_case_exceeds_budget_but_no_file_lost():
    # 予算が非現実的に小さい時: floor 優先 = 合計は予算超過するが file は消えない
    proj = [(f"f{i}.md", 5_000) for i in range(10)]
    out = scale_core_targets(proj, 1_000)
    assert len(out) == 10
    assert all(v == 800 for v in out.values())


def test_empty_projected():
    assert scale_core_targets([], 50_000) == {}
