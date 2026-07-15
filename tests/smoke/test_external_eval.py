"""smoke test: clone_external_eval (月次 第三者 blind 採点ループ)."""
from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(REPO_ROOT / "scripts"))


@pytest.mark.smoke
def test_module_imports():
    import clone_external_eval as mod
    assert hasattr(mod, "sample_turns_for_eval")
    assert hasattr(mod, "generate_form")
    assert hasattr(mod, "import_results")
    assert hasattr(mod, "build_report")
    assert hasattr(mod, "EVAL_AXES")
    assert hasattr(mod, "HTML_HEAD")


@pytest.mark.smoke
def test_eval_axes_has_5():
    """5 軸 (accuracy / authenticity / usefulness / tone / overall) が定義されてる。"""
    import clone_external_eval as mod
    keys = [k for k, _ in mod.EVAL_AXES]
    assert keys == ["accuracy", "authenticity", "usefulness", "tone", "overall"]


@pytest.mark.smoke
def test_is_substantive():
    """応答の長さ / 確認応答チェック。"""
    import clone_external_eval as mod
    assert mod._is_substantive("はい") is False
    assert mod._is_substantive("x" * 100) is True


@pytest.mark.smoke
def test_is_substantive_query():
    """user query 側: 30 字以上 or 業務 keyword。"""
    import clone_external_eval as mod
    assert mod._is_substantive_query("") is False
    assert mod._is_substantive_query("ありがとう") is False  # 短くて keyword なし
    assert mod._is_substantive_query("売上") is True  # 短くても業務 keyword
    assert mod._is_substantive_query("a" * 35) is True  # 30 字以上


@pytest.mark.smoke
def test_html_form_includes_radio_buttons():
    """build_axes_html が 5 段階 radio を生成する。"""
    import clone_external_eval as mod
    html = mod.build_axes_html(1)
    for s in (1, 2, 3, 4, 5):
        assert f'value="{s}"' in html
    # 各 axis が含まれる
    for k, _ in mod.EVAL_AXES:
        assert f't1__{k}' in html


@pytest.mark.smoke
def test_generate_form_with_no_data(tmp_path, monkeypatch):
    """clone_history が空でも crash しない (None を返す)。"""
    import clone_external_eval as mod
    # EVAL_DIR を tmp_path に向ける + clone_history を空に
    monkeypatch.setattr(mod, "EVAL_DIR", tmp_path / "eval")
    monkeypatch.setattr("clone_improve_lib.HISTORY_DIR", tmp_path / "empty_hist")
    result = mod.generate_form("2026-05", n_turns=10, dry_run=False)
    assert result is None  # データ無し


@pytest.mark.smoke
def test_generate_form_with_seed(tmp_path, monkeypatch):
    """サンプル clone_history があれば HTML form が生成される。"""
    import clone_external_eval as mod
    import clone_improve_lib
    hdir = tmp_path / "hist"
    hdir.mkdir()
    # 3 user × 2 pair = 6 候補、5 turn 取れるはず
    records = []
    for u_idx in range(3):
        uid = f"user_{u_idx}"
        for p_idx in range(2):
            base_ts = f"2026-05-{10+p_idx:02d}T{10+u_idx:02d}:00:00+00:00"
            records.append(json.dumps({
                "user_id": uid, "role": "user",
                "text": f"店長の判断軸を教えて、特に売上が低い時の対応について {u_idx}/{p_idx}",
                "timestamp": base_ts,
            }, ensure_ascii=False))
            records.append(json.dumps({
                "user_id": uid, "role": "assistant",
                "text": "売上が低い時の判断軸は、まず原因が一時的か構造的かを見極めること。" * 3,
                "timestamp": base_ts.replace(":00:00", ":00:30"),
            }, ensure_ascii=False))
        (hdir / f"{uid}.jsonl").write_text("\n".join(records[-4:]) + "\n", encoding="utf-8")

    monkeypatch.setattr(clone_improve_lib, "HISTORY_DIR", hdir)
    monkeypatch.setattr(mod, "EVAL_DIR", tmp_path / "eval")

    # 過去 365 日分まで遡って sampling
    form_path = mod.generate_form("2026-05", n_turns=5, days=365, dry_run=False)
    assert form_path is not None
    assert form_path.exists()
    html = form_path.read_text(encoding="utf-8")
    assert "うみやまAI 第三者評価" in html
    assert "2026-05" in html
    # 軸のうち少なくとも "accuracy" の radio がある
    assert 'name="t1__accuracy"' in html
    # responses.json も書かれてる
    assert (tmp_path / "eval" / "2026-05" / "responses.json").exists()


@pytest.mark.smoke
def test_import_results_aggregates(tmp_path, monkeypatch):
    """JSON 評価結果を読み込み、複数 rater 分が array で蓄積される。"""
    import clone_external_eval as mod
    monkeypatch.setattr(mod, "EVAL_DIR", tmp_path / "eval")

    raw1 = {
        "t1__accuracy": "5", "t1__authenticity": "4", "t1__usefulness": "5",
        "t1__tone": "5", "t1__overall": "5", "t1__comment": "完璧",
        "t2__accuracy": "3", "t2__authenticity": "3", "t2__usefulness": "4",
        "t2__tone": "4", "t2__overall": "3",
    }
    raw2 = {
        "t1__accuracy": "4", "t1__authenticity": "5", "t1__usefulness": "4",
        "t1__tone": "4", "t1__overall": "4",
    }
    f1 = tmp_path / "rater_alice.json"
    f1.write_text(json.dumps(raw1, ensure_ascii=False), encoding="utf-8")
    f2 = tmp_path / "rater_bob.json"
    f2.write_text(json.dumps(raw2, ensure_ascii=False), encoding="utf-8")

    mod.import_results(f1, month="2026-05")
    mod.import_results(f2, month="2026-05")

    results_path = tmp_path / "eval" / "2026-05" / "results.json"
    assert results_path.exists()
    data = json.loads(results_path.read_text(encoding="utf-8"))
    assert len(data) == 2
    rater_ids = {r["rater_id"] for r in data}
    assert "rater_alice" in rater_ids
    assert "rater_bob" in rater_ids
    # alice の t1 が 5 axis 揃ってる (JSON read 後は key が str)
    alice = next(r for r in data if r["rater_id"] == "rater_alice")
    assert alice["scores"]["1"]["accuracy"] == 5
    assert alice["comments"]["1"] == "完璧"


@pytest.mark.smoke
def test_build_report_mean_std(tmp_path, monkeypatch):
    """results.json から axis 別 mean / std + 最低スコア turn ranking。"""
    import clone_external_eval as mod
    monkeypatch.setattr(mod, "EVAL_DIR", tmp_path / "eval")

    # 2 rater × 2 turn の手動データ
    out_dir = tmp_path / "eval" / "2026-05"
    out_dir.mkdir(parents=True)
    raters = [
        {"rater_id": "alice", "imported_at": "2026-05-08T10:00:00+09:00",
         "scores": {"1": {"accuracy": 5, "overall": 5}, "2": {"accuracy": 3, "overall": 3}},
         "comments": {}},
        {"rater_id": "bob", "imported_at": "2026-05-08T11:00:00+09:00",
         "scores": {"1": {"accuracy": 4, "overall": 5}, "2": {"accuracy": 2, "overall": 2}},
         "comments": {}},
    ]
    (out_dir / "results.json").write_text(json.dumps(raters), encoding="utf-8")
    (out_dir / "responses.json").write_text(json.dumps(
        [{"user_query": "q1", "bot_response": "a1"}, {"user_query": "q2", "bot_response": "a2"}]
    ), encoding="utf-8")

    summary = mod.build_report("2026-05")
    assert summary["n_raters"] == 2
    assert summary["n_turns"] == 2
    assert summary["axis_mean"]["accuracy"] == 3.5  # (5+3+4+2)/4
    assert summary["axis_mean"]["overall"] == 3.75  # (5+3+5+2)/4
    # lowest turn = turn 2 (overall mean = 2.5)
    assert summary["lowest_turns"][0]["idx"] == 2
    assert summary["lowest_turns"][0]["overall_mean"] == 2.5


@pytest.mark.smoke
def test_build_report_inter_rater_agreement(tmp_path, monkeypatch):
    """★2026-05-21: agreement section が ICC + exact / within_1 / far を出す。"""
    import clone_external_eval as mod
    monkeypatch.setattr(mod, "EVAL_DIR", tmp_path / "eval")

    out_dir = tmp_path / "eval" / "2026-05"
    out_dir.mkdir(parents=True)
    # 3 rater × 3 turn
    raters = [
        {"rater_id": "alice", "scores": {
            "1": {"overall": 5}, "2": {"overall": 3}, "3": {"overall": 4}}, "comments": {}},
        {"rater_id": "bob", "scores": {
            "1": {"overall": 5}, "2": {"overall": 3}, "3": {"overall": 4}}, "comments": {}},
        {"rater_id": "carol", "scores": {
            "1": {"overall": 5}, "2": {"overall": 1}, "3": {"overall": 4}}, "comments": {}},
    ]
    (out_dir / "results.json").write_text(json.dumps(raters), encoding="utf-8")
    (out_dir / "responses.json").write_text(json.dumps(
        [{"user_query": "q1"}, {"user_query": "q2"}, {"user_query": "q3"}]
    ), encoding="utf-8")

    summary = mod.build_report("2026-05")
    assert "agreement" in summary
    ov = summary["agreement"]["overall"]
    assert ov["n_turn_with_2plus_raters"] == 3
    # turn 1, 3 は exact (全 rater 一致)、turn 2 は ばらつき (3,3,1)
    # exact = 2/3
    assert ov["exact_agreement_rate"] == round(2/3, 3)
    # turn 2 だけ far (差 2)
    assert ov["far_2plus_rate"] == round(1/3, 3)
    # ICC は >= 0 (turn 間に差があるので)
    assert ov["icc_approx"] >= 0.0


@pytest.mark.smoke
def test_compute_llm_human_agreement_no_data(tmp_path, monkeypatch):
    """no_data でも crash しない。"""
    import asyncio
    import clone_external_eval as mod
    monkeypatch.setattr(mod, "EVAL_DIR", tmp_path / "eval")
    out = asyncio.run(mod.compute_llm_human_agreement("2026-05"))
    assert out["status"] == "no_data"


@pytest.mark.smoke
def test_compute_llm_human_agreement_phase2(tmp_path, monkeypatch):
    """★2026-06-07 Phase2 実装: sampled turn を judge に通し human と agreement を計算 (judge は mock)。"""
    import asyncio
    import clone_external_eval as mod
    monkeypatch.setattr(mod, "EVAL_DIR", tmp_path / "eval")
    out_dir = tmp_path / "eval" / "2026-05"
    out_dir.mkdir(parents=True)
    # ★2026-07-05 監査 fix: form/import の turn key は 1 始まり (t1__overall → "1")。
    # 旧 fixture は 0 始まり key で「本番では生成され得ないデータ」により off-by-one を隠蔽していた。
    raters = [{"rater_id": "a", "scores": {"1": {"overall": 4}, "2": {"overall": 5}}, "comments": {}}]
    (out_dir / "results.json").write_text(json.dumps(raters), encoding="utf-8")
    (out_dir / "responses.json").write_text(json.dumps(
        [{"user_query": "q1", "bot_response": "r1"}, {"user_query": "q2", "bot_response": "r2"}]
    ), encoding="utf-8")
    # LLM judge を mock。1-based key "1","2" → responses[0],[1] に対応することを検証
    judged_queries = []

    async def _fake_judge(call_llm, extract_json, uq, br, model):
        judged_queries.append(uq)
        return 4.0
    monkeypatch.setattr(mod, "_judge_overall", _fake_judge)

    out = asyncio.run(mod.compute_llm_human_agreement("2026-05"))
    assert out["status"] == "ok"
    assert out["n_overlap_turns"] == 2
    # human key "1" → responses[0] (q1)、key "2" → responses[1] (q2) = turn-aligned
    assert judged_queries == ["q1", "q2"], f"off-by-one: {judged_queries}"
    assert out["mean_abs_diff"] == 0.5 and out["within_1_rate"] == 1.0


@pytest.mark.smoke
def test_pearson_helper():
    import clone_external_eval as mod
    assert mod._pearson([1, 2, 3], [1, 2, 3]) == pytest.approx(1.0)
    assert mod._pearson([1, 2, 3], [3, 2, 1]) == pytest.approx(-1.0)
    assert mod._pearson([1, 1], [5, 5]) is None  # 分散0 → None


@pytest.mark.smoke
def test_sample_turns_diversity(tmp_path, monkeypatch):
    """同じ user / 同じ日から 5 件以上取らない。"""
    import clone_external_eval as mod
    import clone_improve_lib
    hdir = tmp_path / "hist"
    hdir.mkdir()
    # u1 が 10 turn 連続 = max 5 までしか取らないはず
    records = []
    for i in range(10):
        ts = f"2026-05-{10:02d}T{10+i:02d}:00:00+00:00"
        records.append(json.dumps({
            "user_id": "u1", "role": "user",
            "text": f"投資判断について意見が欲しいです、ROI 2 年での評価でいいですか {i}",
            "timestamp": ts,
        }, ensure_ascii=False))
        records.append(json.dumps({
            "user_id": "u1", "role": "assistant",
            "text": "ROI 2 年は OWNDAYS の基本軸として正しい。" * 5,
            "timestamp": ts.replace(":00:00", ":00:30"),
        }, ensure_ascii=False))
    (hdir / "u1.jsonl").write_text("\n".join(records) + "\n", encoding="utf-8")

    monkeypatch.setattr(clone_improve_lib, "HISTORY_DIR", hdir)
    turns = mod.sample_turns_for_eval(days=365, n_turns=20)
    # u1 から 5 件 + 同日制限で 4 件 = min(5, 4) = 4 件まで
    assert len(turns) <= 5
