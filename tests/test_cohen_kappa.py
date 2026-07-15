"""clone_external_eval の weighted Cohen's κ test (LLMOps G3: judge systematic bias 検出)。

★2026-06-08: pearson は systematic offset (judge が一貫して甘い/辛い) を見逃す (r=1.0 になる)。
weighted κ がそれを捉えることを固定する。
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))

from clone_external_eval import _weighted_cohen_kappa, _pearson  # noqa: E402


def test_perfect_agreement_kappa_one():
    assert _weighted_cohen_kappa([1, 2, 3, 4, 5], [1, 2, 3, 4, 5]) == 1.0


def test_systematic_bias_caught_by_kappa_not_pearson():
    # judge が一貫して +1 甘い: pearson は完全相関 (見逃す)、κ は <1 (捉える)
    h = [1, 2, 3, 4, 1, 2, 3, 4]
    l = [2, 3, 4, 5, 2, 3, 4, 5]
    assert abs(_pearson(h, l) - 1.0) < 1e-9      # pearson = 1.0 = 見逃す
    k = _weighted_cohen_kappa(h, l)
    assert k is not None and k < 1.0             # κ < 1 = systematic bias を捉える
    assert k < 0.9


def test_reverse_correlation_low_kappa():
    k = _weighted_cohen_kappa([1, 5, 2, 4], [5, 1, 4, 2])
    assert k is not None and k < 0


def test_float_human_mean_rounded():
    # human_mean は float (rater 平均) → 丸めて category 化
    assert _weighted_cohen_kappa([3.4, 4.6, 2.5], [3, 5, 2]) == 1.0


def test_too_few_pairs_none():
    assert _weighted_cohen_kappa([3], [3]) is None
    assert _weighted_cohen_kappa([], []) is None


def test_clamps_out_of_range():
    # 範囲外 (0 や 6) でも例外を出さず clamp
    k = _weighted_cohen_kappa([0, 6, 3], [1, 5, 3])
    assert k is not None
