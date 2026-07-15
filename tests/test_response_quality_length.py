"""tests/test_response_quality_length.py — ★2026-07-02 監査 P1d

compute_length_score の校正を固定する。旧実装 (比のみ) は短い挨拶への gold 同等の短応答を
比のみで length=1-2 と誤判定し、verdict=min(3軸) 経由で 97% を偽 degraded にしていた。
絶対字数 floor 導入後の期待値を回帰テストで固定する (純関数、依存なし)。
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))

from clone_response_quality_judge import compute_length_score  # noqa: E402


def test_short_reply_to_short_query_is_not_penalized():
    # 5字挨拶への 57字の良質な短応答 = gold 同等。旧実装は比 11x で 3、修正後は 5。
    assert compute_length_score(5, 57) == 5
    # 3字への 60字 (旧: 比 20x → 2 = degraded)。短応答は過剰たりえない → 5。
    assert compute_length_score(3, 60) == 5


def test_absolute_floor_holds_regardless_of_ratio():
    # 200字以下は query が何字でも過剰でない
    assert compute_length_score(1, 200) == 5
    assert compute_length_score(10, 150) == 5


def test_genuine_over_verbosity_is_caught():
    # 短い問いへの長文 essay = AI 過剰。低スコア (degraded 域 ≤2)。
    assert compute_length_score(20, 800) <= 2
    assert compute_length_score(10, 900) == 1


def test_deep_query_deep_answer_is_ok():
    # 深い問い (200字) への厚い答え (1600字、比 8x) は過剰でない (≥3 = not degraded)。
    assert compute_length_score(200, 1600) >= 3


def test_factual_medium_answer_survives():
    # 事実問「VMVって?」(6字) への 400字定義 (比 66x) も中尺 = 救済 (≥4)。
    assert compute_length_score(6, 400) >= 4


def test_monotonic_by_absolute_bands():
    # 同じ query 長でも応答が長くなるほどスコアは上がらない (単調非増加)
    q = 10
    prev = 5
    for r in (100, 250, 500, 800, 1500, 3000):
        s = compute_length_score(q, r)
        assert s <= prev
        prev = s


def test_zero_query_chars_no_crash():
    # query 0 字 (異常系) でも ZeroDivision で落ちない
    assert compute_length_score(0, 50) == 5
    assert 1 <= compute_length_score(0, 5000) <= 5
