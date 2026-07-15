"""smoke test: clone_alignment_trial — 100 件アラインメントトライアル"""
from __future__ import annotations

import importlib
import json
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(REPO_ROOT / "scripts"))


@pytest.fixture
def trial_mod(tmp_path, monkeypatch):
    """BRAIN_APP_ROOT=tmp で reload。"""
    monkeypatch.setenv("BRAIN_APP_ROOT", str(tmp_path))
    if "clone_alignment_trial" in sys.modules:
        importlib.reload(sys.modules["clone_alignment_trial"])
    import clone_alignment_trial as mod  # type: ignore
    return mod, tmp_path


@pytest.fixture
def sample_questions_md(tmp_path):
    """テスト用に小さい questions.md を書く。"""
    content = """# Test questions

## store-001 (店長 / TSA)
店長です。TSA が落ちてます。どうすれば?

**expected_axes**:
- 3 軸で分けて見る
- 即対策じゃない
- 開かれた問い

---

## sv-001 (SV / エリア戦略)
SV です。担当エリアで売上格差が広がってます。

**expected_axes**:
- 全社利益で判断
- 配分の優先順位

---

## hq-sales-001 (営業本部 / 出店判断)
営業本部です。出店候補で迷ってます。

**expected_axes**:
- ROI 2 年
- 商圏成長性
"""
    target = tmp_path / "data" / "brain" / "clone_improve" / "alignment_trial"
    target.mkdir(parents=True, exist_ok=True)
    p = target / "questions.md"
    p.write_text(content, encoding="utf-8")
    return p


@pytest.mark.smoke
def test_module_imports(trial_mod):
    mod, _ = trial_mod
    assert hasattr(mod, "parse_questions")
    assert hasattr(mod, "run_trial")
    assert hasattr(mod, "generate_html")
    assert hasattr(mod, "save_run")
    assert hasattr(mod, "load_run")
    assert hasattr(mod, "ingest_review")
    assert hasattr(mod, "diff_runs")
    assert hasattr(mod, "count_by_role")


@pytest.mark.smoke
def test_parse_questions_simple_role(trial_mod, sample_questions_md):
    """role: 店長 / SV / AM 等 (単純な role)。"""
    mod, _ = trial_mod
    qs = mod.parse_questions(sample_questions_md)
    assert len(qs) == 3
    assert qs[0]["id"] == "store-001"
    assert qs[0]["role"] == "店長"
    assert qs[0]["category"] == "TSA"
    assert "店長です" in qs[0]["scenario"]
    assert len(qs[0]["expected_axes"]) == 3
    assert qs[1]["id"] == "sv-001"
    assert qs[1]["role"] == "SV"


@pytest.mark.smoke
def test_parse_questions_compound_id(trial_mod, sample_questions_md):
    """hq-sales-001 のような複合 prefix (= hq-sales-) も parse される。"""
    mod, _ = trial_mod
    qs = mod.parse_questions(sample_questions_md)
    hq = [q for q in qs if q["id"].startswith("hq-")]
    assert len(hq) == 1
    assert hq[0]["id"] == "hq-sales-001"
    assert hq[0]["role"] == "営業本部"


@pytest.mark.smoke
def test_count_by_role(trial_mod, sample_questions_md):
    mod, _ = trial_mod
    qs = mod.parse_questions(sample_questions_md)
    counts = mod.count_by_role(qs)
    assert counts == {"店長": 1, "SV": 1, "営業本部": 1}


@pytest.mark.smoke
def test_real_questions_parse():
    """本番の questions.md (135 件、店舗 70 + 本部 65) が正しく parse される。

    2026-05-21 構成変遷:
    - 初版 100 件
    - + ブランド戦略本部 5 件 = 105 件
    - + EC / DX / 社長室 / 経営企画 / 店舗開発 / FC 各 5 件 = 135 件
    """
    real_path = REPO_ROOT / "docs" / "alignment_trial" / "questions.md"
    if not real_path.exists():
        pytest.skip("real questions.md not yet committed")
    if "clone_alignment_trial" in sys.modules:
        importlib.reload(sys.modules["clone_alignment_trial"])
    import clone_alignment_trial as mod  # type: ignore
    qs = mod.parse_questions(real_path)
    assert len(qs) == 135, f"expected 135 questions, got {len(qs)}"
    counts = mod.count_by_role(qs)
    # 海山指定の構成: 店舗 70 / 本部 65
    store_total = counts.get("店長", 0) + counts.get("SV", 0) + counts.get("AM", 0) + counts.get("スタッフ", 0)
    hq_total = sum(v for k, v in counts.items()
                    if k not in ("店長", "SV", "AM", "スタッフ"))
    assert store_total == 70, f"店舗系: {store_total} (期待 70)"
    assert hq_total == 65, f"本部系: {hq_total} (期待 65)"
    # 新追加カテゴリの存在確認
    for role in ("ブランド戦略本部", "EC事業本部", "デジタライゼーション",
                  "社長室", "経営企画室", "店舗開発", "フランチャイズ本部"):
        assert counts.get(role, 0) == 5, f"{role} 5 件が無い (got {counts.get(role, 0)})"


@pytest.mark.smoke
def test_generate_html_includes_all(trial_mod):
    """generate_html が全 question 分の section を出す。"""
    mod, _ = trial_mod
    results = [
        {"id": "store-001", "role": "店長", "category": "TSA",
         "scenario": "店長です。TSA が落ちてます。", "expected_axes": ["軸1", "軸2"],
         "response": "3 つに分けて見たい。", "model": "smart",
         "ts": "2026-05-21T10:00:00"},
        {"id": "sv-001", "role": "SV", "category": "戦略",
         "scenario": "SV です。", "expected_axes": ["軸A"],
         "response": "テスト応答。", "model": "smart",
         "ts": "2026-05-21T10:00:00"},
    ]
    html = mod.generate_html(results, tag="test")
    assert "store-001" in html
    assert "sv-001" in html
    assert "店長" in html
    assert "TSA が落ちてます" in html
    assert "3 つに分けて見たい" in html
    assert "exportReview()" in html  # JS export 機能


@pytest.mark.smoke
def test_save_and_load_run(trial_mod):
    mod, tmp_path = trial_mod
    results = [{"id": "store-001", "role": "店長", "category": "x",
                "scenario": "s", "expected_axes": [], "response": "r",
                "model": "smart", "ts": "2026-05-21T10:00:00"}]
    json_path = mod.save_run(results, "test_run_1", tag="v1-test")
    assert json_path.exists()
    loaded = mod.load_run("test_run_1")
    assert loaded["run_id"] == "test_run_1"
    assert loaded["tag"] == "v1-test"
    assert len(loaded["results"]) == 1
    # HTML も生成された
    html_path = tmp_path / "data" / "brain" / "clone_improve" / "alignment_trial" / "runs" / "test_run_1.html"
    assert html_path.exists()


@pytest.mark.smoke
def test_ingest_review(trial_mod):
    """JSON form export を取り込んで run に統合 (★2026-05-22 新 schema)。"""
    mod, tmp_path = trial_mod
    # 1. seed: run を作る
    results = [
        {"id": "store-001", "role": "店長", "category": "TSA",
         "scenario": "TSA 落ちてます", "expected_axes": ["軸1", "軸2"],
         "response": "AI 元応答",
         "model": "smart", "ts": "2026-05-21T10:00:00"},
        {"id": "sv-001", "role": "SV", "category": "戦略",
         "scenario": "SV 質問", "expected_axes": ["軸A"], "response": "AI 元応答 2",
         "model": "smart", "ts": "2026-05-21T10:00:00"},
    ]
    mod.save_run(results, "test_run_2", tag="v1-test")

    # 2. レビュー JSON (= browser から export された形式)
    review = {
        "store-001__verdict": "ok",
        "store-001__question": "TSA 落ちてます",  # 同じ (= edit してない)
        "store-001__response": "AI 元応答",        # 同じ (= 採用)
        "sv-001__verdict": "fix",
        "sv-001__question": "SV 質問 (修正版)",   # 修正
        "sv-001__response": "海山の理想応答",      # 修正
    }
    review_path = tmp_path / "review.json"
    review_path.write_text(json.dumps(review, ensure_ascii=False), encoding="utf-8")

    # 3. 取り込み
    summary = mod.ingest_review(review_path, "test_run_2")
    assert summary["verdicts"]["ok"] == 1
    assert summary["verdicts"]["fix"] == 1
    assert summary["verdicts"]["reject"] == 0
    # 修正カウント
    assert summary["n_question_edited"] == 1  # sv-001 だけ修正
    assert summary["n_response_edited"] == 1

    # 4. _reviewed.json に統合されてる
    reviewed_path = tmp_path / "data" / "brain" / "clone_improve" / "alignment_trial" / "runs" / "test_run_2_reviewed.json"
    assert reviewed_path.exists()
    reviewed = json.loads(reviewed_path.read_text(encoding="utf-8"))

    # store-001: 修正なし、edited_question / edited_response が立たない
    r0 = next(r for r in reviewed["results"] if r["id"] == "store-001")
    assert r0["verdict"] == "ok"
    assert "edited_question" not in r0
    assert "edited_response" not in r0

    # sv-001: 修正あり
    r1 = next(r for r in reviewed["results"] if r["id"] == "sv-001")
    assert r1["verdict"] == "fix"
    assert r1["edited_question"] == "SV 質問 (修正版)"
    assert r1["edited_response"] == "海山の理想応答"


@pytest.mark.smoke
def test_diff_runs(trial_mod):
    """2 runs の diff: 同じ id で response が変わった件数を集計。"""
    mod, _ = trial_mod
    # run A
    r_a = [
        {"id": "store-001", "role": "店長", "category": "TSA",
         "scenario": "s", "expected_axes": [], "response": "古い応答",
         "model": "smart", "ts": "1"},
        {"id": "sv-001", "role": "SV", "category": "x",
         "scenario": "s2", "expected_axes": [], "response": "応答 b",
         "model": "smart", "ts": "1"},
    ]
    # run B: store-001 は応答変わった、sv-001 は同じ
    r_b = [
        {"id": "store-001", "role": "店長", "category": "TSA",
         "scenario": "s", "expected_axes": [], "response": "新しい応答 (改善版)",
         "model": "smart", "ts": "2"},
        {"id": "sv-001", "role": "SV", "category": "x",
         "scenario": "s2", "expected_axes": [], "response": "応答 b",
         "model": "smart", "ts": "2"},
    ]
    mod.save_run(r_a, "diff_a")
    mod.save_run(r_b, "diff_b")
    report = mod.diff_runs("diff_a", "diff_b")
    assert report["n_common"] == 2
    assert report["n_changed"] == 1
    assert report["unchanged_rate"] == 0.5


@pytest.mark.smoke
def test_trial_prompt_prefix_includes_constraints(trial_mod):
    """TRIAL_PROMPT_PREFIX が砕けたトーン + 短め指示を含む。"""
    mod, _ = trial_mod
    assert hasattr(mod, "TRIAL_PROMPT_PREFIX")
    prefix = mod.TRIAL_PROMPT_PREFIX
    # 主要キーワード
    assert "砕けたトーン" in prefix
    assert "敬語" in prefix
    # 短め指示
    assert "150-300" in prefix or "字" in prefix


@pytest.mark.smoke
def test_html_has_editable_question_and_response(trial_mod):
    """★2026-05-22: HTML が question/response を editable textarea で出す。"""
    mod, _ = trial_mod
    results = [{"id": "test-001", "role": "店長", "category": "TSA",
                "scenario": "店長です。TSA 落ちてます。",
                "expected_axes": ["3 軸で分けて見る"],
                "response": "TSA 落ちてるか、3 軸で見たい。",
                "model": "smart", "ts": "t"}]
    out_html = mod.generate_html(results, tag="test")

    # editable textarea 2 つ (= question / response)
    assert 'name="test-001__question"' in out_html
    assert 'name="test-001__response"' in out_html
    assert "editable q-box" in out_html
    assert "editable r-box" in out_html

    # 中身は scenario / response がそのまま入る
    assert "店長です。TSA 落ちてます。" in out_html
    assert "TSA 落ちてるか、3 軸で見たい。" in out_html

    # editable focus スタイル
    assert ".editable:focus" in out_html


@pytest.mark.smoke
def test_html_no_comment_or_axis_score(trial_mod):
    """★2026-05-22: コメント欄と軸スコア radio が廃止されてる。"""
    mod, _ = trial_mod
    results = [{"id": "x", "role": "r", "category": "c",
                "scenario": "s", "expected_axes": ["軸1", "軸2"],
                "response": "resp",
                "model": "smart", "ts": "t"}]
    out_html = mod.generate_html(results, tag="test")
    # コメント欄が無い
    assert 'name="x__comment"' not in out_html
    assert "comment-box" not in out_html
    # 軸スコア radio が無い
    assert 'name="x__axis0"' not in out_html
    assert 'name="x__axis1"' not in out_html
    # verdict ラジオはまだある
    assert 'name="x__verdict"' in out_html


@pytest.mark.smoke
def test_diff_runs_handles_partial_overlap(trial_mod):
    """run A と B で id 集合が違っても crash しない (= 共通のみ集計)。"""
    mod, _ = trial_mod
    r_a = [
        {"id": "store-001", "role": "店長", "category": "x",
         "scenario": "s", "expected_axes": [], "response": "a",
         "model": "smart", "ts": "1"},
        {"id": "store-002", "role": "店長", "category": "x",
         "scenario": "s2", "expected_axes": [], "response": "a2",
         "model": "smart", "ts": "1"},
    ]
    r_b = [
        {"id": "store-001", "role": "店長", "category": "x",
         "scenario": "s", "expected_axes": [], "response": "b",
         "model": "smart", "ts": "2"},
        {"id": "store-003", "role": "店長", "category": "x",
         "scenario": "s3", "expected_axes": [], "response": "b3",
         "model": "smart", "ts": "2"},
    ]
    mod.save_run(r_a, "partial_a")
    mod.save_run(r_b, "partial_b")
    report = mod.diff_runs("partial_a", "partial_b")
    assert report["n_common"] == 1  # store-001 のみ共通
    assert report["n_changed"] == 1
