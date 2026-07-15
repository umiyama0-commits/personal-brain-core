"""deploy_eval_gate の純関数 test (regression 判定 / summary parse)。

★2026-06-08 システム評価 LLMOps G1: deploy 後 eval gate。HTTP/docker は局所テスト不可だが、
regression 判定ロジックと summary parse は純関数なので fixture で守る (= 誤検知で正常 deploy を
止めない / 本物の regression を見逃さない の両方)。
"""
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))

from deploy_eval_gate import decide, read_summaries, last_combined_rate  # noqa: E402


def test_decide_regression():
    verdict, _ = decide(current=0.70, baseline=0.90, threshold=0.9)  # floor 0.81
    assert verdict == "regression"


def test_decide_ok_above_floor():
    verdict, _ = decide(current=0.88, baseline=0.90, threshold=0.9)  # floor 0.81
    assert verdict == "ok"


def test_decide_ok_exactly_at_floor():
    # current == floor は ok (>=)
    verdict, _ = decide(current=0.81, baseline=0.90, threshold=0.9)
    assert verdict == "ok"


def test_decide_improvement_is_ok():
    verdict, _ = decide(current=0.95, baseline=0.90, threshold=0.9)
    assert verdict == "ok"


def test_decide_inconclusive_when_no_current():
    verdict, _ = decide(current=None, baseline=0.90, threshold=0.9)
    assert verdict == "inconclusive"


def test_decide_inconclusive_when_no_baseline():
    verdict, _ = decide(current=0.80, baseline=None, threshold=0.9)
    assert verdict == "inconclusive"


def test_decide_inconclusive_when_baseline_zero():
    verdict, _ = decide(current=0.80, baseline=0.0, threshold=0.9)
    assert verdict == "inconclusive"


def test_read_summaries_skips_garbage(tmp_path):
    f = tmp_path / "eval_summary_v1.jsonl"
    f.write_text(
        json.dumps({"combined_pass_rate": 0.8}) + "\n"
        + "this is not json\n"
        + json.dumps({"combined_pass_rate": 0.85}) + "\n",
        encoding="utf-8",
    )
    out = read_summaries(f)
    assert len(out) == 2  # 壊れた行は skip
    assert last_combined_rate(out) == 0.85  # 最新行


def test_last_combined_rate_empty():
    assert last_combined_rate([]) is None


def test_read_summaries_missing_file(tmp_path):
    assert read_summaries(tmp_path / "nope.jsonl") == []


# ─── ★G4: 統計的有意性 (Wilson 上側信頼限界) ──────────────────────────

def test_g4_small_sample_flake_not_regression():
    """点推定では floor を割るが、小サンプルなら flake と区別不能 → regression にしない。"""
    # baseline 0.8 × 0.9 = floor 0.72。rate 0.7 (7/10) は点推定で割るが n=10 は有意でない
    from deploy_eval_gate import decide
    verdict, _ = decide(0.7, 0.8, 0.9, current_pass=7, current_n=10)
    assert verdict == "ok"   # 誤 rollback を防ぐ


def test_g4_large_sample_clear_regression():
    """大サンプルで明確に劣化 → Wilson 上側も floor を割る → regression。"""
    from deploy_eval_gate import decide
    verdict, _ = decide(0.5, 0.8, 0.9, current_pass=50, current_n=100)
    assert verdict == "regression"


def test_g4_falls_back_to_point_estimate_when_no_n():
    """n 不明なら従来の点推定 (後方互換)。"""
    from deploy_eval_gate import decide
    assert decide(0.5, 0.8, 0.9)[0] == "regression"
    assert decide(0.75, 0.8, 0.9)[0] == "ok"


def test_wilson_upper_bounds():
    from deploy_eval_gate import _wilson_upper
    assert _wilson_upper(10, 10) == 1.0          # 全部 pass → 上側 1.0
    assert 0.0 <= _wilson_upper(0, 10) <= 0.5    # 全部 fail → 上側は低い
    # 小 n ほど上側が広い (不確実性大)
    assert _wilson_upper(7, 10) > _wilson_upper(70, 100)


# ─── ★評価#1: warn 期間の verdict ログ (誤検知率の実測データ) ──────────────

def test_log_verdict_writes_record(tmp_path, monkeypatch):
    """gate 判定が jsonl に正しく記録される (block 切替の誤検知率測定の素材)。"""
    import deploy_eval_gate as g
    monkeypatch.setattr(g, "VERDICT_LOG", tmp_path / "verdicts.jsonl")
    g._log_verdict("regression", 0.72, 0.90, 0.9, 30, 21, "warn", "abc1234")
    rec = json.loads((tmp_path / "verdicts.jsonl").read_text().strip())
    assert rec["verdict"] == "regression"
    assert rec["mode"] == "warn"
    assert rec["commit"] == "abc1234"
    assert rec["floor"] == round(0.90 * 0.9, 4)  # = 0.81
    assert rec["current_n"] == 30 and rec["current_pass"] == 21
    assert "ts" in rec


def test_log_verdict_appends(tmp_path, monkeypatch):
    """複数 deploy で append される (1 行 1 判定)。"""
    import deploy_eval_gate as g
    monkeypatch.setattr(g, "VERDICT_LOG", tmp_path / "v.jsonl")
    g._log_verdict("ok", 0.9, 0.9, 0.9, 30, 28, "warn", "c1")
    g._log_verdict("regression", 0.6, 0.9, 0.9, 30, 18, "warn", "c2")
    lines = (tmp_path / "v.jsonl").read_text().strip().splitlines()
    assert len(lines) == 2
    assert json.loads(lines[1])["commit"] == "c2"


def test_log_verdict_fail_safe(monkeypatch):
    """書込先が不正でも例外を投げない (gate を壊さない fail-safe)。"""
    import deploy_eval_gate as g
    monkeypatch.setattr(g, "VERDICT_LOG", Path("/nonexistent_root_xyz/v.jsonl"))
    # 例外が漏れないこと (raise しなければ pass)
    g._log_verdict("ok", 0.9, 0.9, 0.9, 30, 28, "warn", "c1")
