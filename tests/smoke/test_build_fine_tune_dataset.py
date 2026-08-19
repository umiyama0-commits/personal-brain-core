"""smoke test: scripts/build_fine_tune_dataset.py (★2026-05-23 海山指示 A の準備)

source 3 種 (clone_history / alignment_trial / alignment_history) からの pair 抽出 +
品質フィルタ + OpenAI fine-tune 形式変換の sanity check。
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(REPO / "scripts"))


@pytest.fixture
def isolated_brain_root(tmp_path, monkeypatch):
    """data/brain ツリーを tmp に作って、本物に触らず test 走らせる。"""
    monkeypatch.setenv("BRAIN_APP_ROOT", str(tmp_path))
    (tmp_path / "data" / "brain" / "clone_history").mkdir(parents=True)
    (tmp_path / "data" / "brain" / "clone_improve" / "alignment_trial" / "runs").mkdir(parents=True)
    (tmp_path / "data" / "brain" / "clone_improve" / "response_quality").mkdir(parents=True)
    (tmp_path / "data" / "brain" / "fine_tune").mkdir(parents=True)
    # module reload で BRAIN_APP_ROOT 反映
    import importlib
    import build_fine_tune_dataset
    importlib.reload(build_fine_tune_dataset)
    return tmp_path / "data" / "brain"


# ─── _is_substantive ─────────────
@pytest.mark.smoke
def test_is_substantive_filters_fallback_and_short():
    from build_fine_tune_dataset import _is_substantive
    assert _is_substantive("") is False
    assert _is_substantive("はい。") is False  # 短すぎ
    assert _is_substantive("お休みをいただいてます。しばらく経ってから") is False  # fallback
    assert _is_substantive("申し訳ありません。少し時間を置いて") is False
    assert _is_substantive("[error] internal") is False
    # 普通の応答 (= 30 字以上)
    assert _is_substantive("武蔵小山パルムは客数 12 / 売上 38 万 / 単価 31700 円。") is True
    assert _is_substantive("そうね、元気出ない時あるよね。無理に上げようとしないこと、大事だと思う。") is True


# ─── source 1: clone_history pair 抽出 ─────────────
@pytest.mark.smoke
def test_iter_history_pairs_basic(isolated_brain_root):
    """clone_history/<user_id>.jsonl から user-assistant pair を抽出。"""
    user_file = isolated_brain_root / "clone_history" / "u1.jsonl"
    records = [
        {"timestamp": "2026-05-22T10:00:00", "user_id": "u1", "role": "user", "text": "質問 1"},
        {"timestamp": "2026-05-22T10:00:05", "user_id": "u1", "role": "assistant",
         "text": "これは 30 字超え substantive な応答" * 2},
        {"timestamp": "2026-05-22T10:01:00", "user_id": "u1", "role": "user", "text": "質問 2"},
        {"timestamp": "2026-05-22T10:01:05", "user_id": "u1", "role": "assistant",
         "text": "もう 1 つの substantive 応答 これも 30 字超え"},
    ]
    user_file.write_text("\n".join(json.dumps(r, ensure_ascii=False) for r in records), encoding="utf-8")

    from build_fine_tune_dataset import iter_history_pairs
    pairs = list(iter_history_pairs())
    assert len(pairs) == 2
    for p in pairs:
        assert "user" in p and "assistant" in p
        assert p["source"] == "clone_history"


@pytest.mark.smoke
def test_iter_history_pairs_excludes_fallback(isolated_brain_root):
    """fallback 文言の bot 応答は除外される。"""
    user_file = isolated_brain_root / "clone_history" / "u1.jsonl"
    records = [
        {"timestamp": "2026-05-22T10:00:00", "user_id": "u1", "role": "user", "text": "質問"},
        {"timestamp": "2026-05-22T10:00:05", "user_id": "u1", "role": "assistant",
         "text": "お休みをいただいてます。しばらく経ってから再度試して。"},
    ]
    user_file.write_text("\n".join(json.dumps(r, ensure_ascii=False) for r in records), encoding="utf-8")

    from build_fine_tune_dataset import iter_history_pairs
    pairs = list(iter_history_pairs())
    assert len(pairs) == 0


# ─── source 2: alignment_trial review ─────────────
@pytest.mark.smoke
def test_iter_alignment_trial_includes_ok_and_fix(isolated_brain_root):
    """verdict=ok と fix (+edited_response) を採用、reject は除外。"""
    run_file = isolated_brain_root / "clone_improve" / "alignment_trial" / "runs" / "test_run_reviewed.json"
    run = {
        "run_id": "test_run",
        "results": [
            {
                "id": "q1", "scenario": "シナリオ 1 の質問内容",
                "response": "うみやまAI 応答 1 (30 字以上)" * 2,
                "verdict": "ok",
            },
            {
                "id": "q2", "scenario": "シナリオ 2",
                "response": "raw 応答",
                "edited_response": "海山書き直し版応答 (30 字超え) " * 2,
                "verdict": "fix",
            },
            {
                "id": "q3", "scenario": "シナリオ 3",
                "response": "ダメな応答",
                "verdict": "reject",
            },
            {
                "id": "q4", "scenario": "シナリオ 4",
                "response": "未レビュー",
                "verdict": "",
            },
        ],
    }
    run_file.write_text(json.dumps(run, ensure_ascii=False), encoding="utf-8")

    from build_fine_tune_dataset import iter_alignment_trial_pairs
    pairs = list(iter_alignment_trial_pairs())
    # ok + fix の 2 件のみ採用
    assert len(pairs) == 2
    sources = {p["source"] for p in pairs}
    assert any("ok" in s for s in sources)
    assert any("fix" in s for s in sources)


# ─── source 3: alignment_history ─────────────
@pytest.mark.smoke
def test_iter_alignment_history_pairs(isolated_brain_root):
    """alignment_history.json の 100 問形式から抽出。"""
    hist = isolated_brain_root / "alignment_history.json"
    data = [
        # 本番 schema (★2026-05-23): answer_summary が本文
        {"date": "2026-04-16", "category": "orientation",
         "question": "Q1 海山さんは何を大事にしてる?",
         "intent": "価値観の理解",
         "answer_summary": "海山として書いた answer 30 字超え" * 2},
        # legacy schema fallback: answer field
        {"question": "Q legacy", "answer": "legacy field の answer (30 字超え)" * 2},
        # 短すぎ → 除外
        {"question": "Q short", "answer_summary": "短い"},
    ]
    hist.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")

    from build_fine_tune_dataset import iter_alignment_history_pairs
    pairs = list(iter_alignment_history_pairs())
    assert len(pairs) == 2  # answer_summary 採用 1 + answer (legacy) 採用 1、短い 1 件除外
    assert all(p["source"] == "alignment_history" for p in pairs)
    # answer_summary 経路で日付 + category + intent が引き継がれる
    target = next(p for p in pairs if "Q1" in p["user"])
    assert target["ts"] == "2026-04-16"
    assert target["category"] == "orientation"
    assert target["intent"] == "価値観の理解"


# ─── source 4: wiki/interview/ Vapi 蒸留採用済 (★2026-05-23 追加) ─────────────
@pytest.mark.smoke
def test_iter_interview_wiki_pairs(isolated_brain_root):
    """wiki/interview/{category}.md の bullet 列挙形式から pair 化 (★2026-05-23 真因 fix)。"""
    iv_dir = isolated_brain_root / "wiki" / "interview"
    iv_dir.mkdir(parents=True)
    # 本番形式: bullet 列挙
    (iv_dir / "biography.md").write_text(
        "---\nupdated: 2026-05-20\nclone_visibility: private\n---\n"
        "# biography (雑談アラインメント由来)\n\n"
        "海山が AI と雑談した内容から蒸留。\n\n"
        "- [2026-05-20] (high) 20 代前半でメガネ業界に入った経緯について。 — 出典: 「...」\n"
        "- [2026-05-18] (medium) 前職での失敗から学んだこと。\n",
        encoding="utf-8",
    )
    (iv_dir / "value-roots.md").write_text(
        "# value-roots\n\n"
        "- [2026-05-19] (high) 信頼と速度を最優先する価値観。\n",
        encoding="utf-8",
    )

    from build_fine_tune_dataset import iter_interview_wiki_pairs
    pairs = list(iter_interview_wiki_pairs())
    assert len(pairs) == 3
    sources = {p["source"] for p in pairs}
    assert "wiki_interview:biography" in sources
    assert "wiki_interview:value-roots" in sources
    # user query が category ベース
    bio = next(p for p in pairs if p["source"] == "wiki_interview:biography" and "20 代" in p["assistant"])
    assert "経歴" in bio["user"]
    # ts が bullet から抽出される
    assert bio["ts"] == "2026-05-20"
    # confidence も取れる
    assert bio.get("confidence") == "high"


@pytest.mark.smoke
def test_iter_raw_notes_alignment_pairs(isolated_brain_root):
    """raw/notes/ から alignment_* / align_* prefix のみ拾う。"""
    notes_dir = isolated_brain_root / "raw" / "notes"
    notes_dir.mkdir(parents=True)
    # 採用される: alignment 系
    (notes_dir / "alignment_100q_v2_2026-05-11.md").write_text(
        "---\ntype: alignment\n---\n"
        "# 100 問アラインメント v2\n\n"
        "海山が答えた 100 問の deliberate なメモ本文。30 字を超える内容。",
        encoding="utf-8",
    )
    (notes_dir / "align_org_owndays_2026-05-13.md").write_text(
        "# 組織 alignment メモ\n\n"
        "OWNDAYS 組織の理想形について海山が書いた deliberate メモ。",
        encoding="utf-8",
    )
    # 除外される: apple_notes / lineworks / gdrive 等
    (notes_dir / "apple_notes_2026-04-27_batch0.md").write_text(
        "海山の Apple Notes import、PII リスク有 (= 除外)",
        encoding="utf-8",
    )
    (notes_dir / "lineworks_南部_2026-04-24.md").write_text(
        "社員間のチャット、海山発言含まれるが第三者著作混在 (= 除外)",
        encoding="utf-8",
    )

    from build_fine_tune_dataset import iter_raw_notes_alignment_pairs
    pairs = list(iter_raw_notes_alignment_pairs())
    assert len(pairs) == 2
    sources = [p["source"] for p in pairs]
    assert "raw_notes:alignment" in sources
    assert "raw_notes:align" in sources


@pytest.mark.smoke
def test_collect_pairs_includes_high_quality_sources(isolated_brain_root):
    """wiki_interview / raw_notes は採点なしでも常に採用される。"""
    # alignment_history (= 既存高品質 source)
    hist = isolated_brain_root / "alignment_history.json"
    hist.write_text(
        json.dumps([{
            "date": "2026-04-16", "category": "orientation",
            "question": "Q", "answer_summary": "alignment_history 海山直答" * 2,
        }], ensure_ascii=False),
        encoding="utf-8",
    )
    # wiki/interview/biography.md (= 新規高品質 source、bullet 形式)
    iv_dir = isolated_brain_root / "wiki" / "interview"
    iv_dir.mkdir(parents=True)
    (iv_dir / "biography.md").write_text(
        "# bio\n\n"
        "- [2026-05-20] (high) 本文 = 海山蒸留採用済の本人発言要約。\n",
        encoding="utf-8",
    )
    # raw/notes/alignment_*.md (= 新規高品質 source)
    notes_dir = isolated_brain_root / "raw" / "notes"
    notes_dir.mkdir(parents=True)
    (notes_dir / "alignment_x_2026-04-23.md").write_text(
        "# title\n\n海山 deliberate メモ (= 30 字以上)。",
        encoding="utf-8",
    )

    from build_fine_tune_dataset import collect_pairs
    # min_quality=3 (= scored 求める strict)、include_unscored=False
    pairs = collect_pairs(include_unscored=False, min_quality=3)
    # 3 高品質 source 全部採用される (= 採点無くても)
    sources = {p["source"].split(":")[0] for p in pairs}
    assert "alignment_history" in sources
    assert "wiki_interview" in sources
    assert "raw_notes" in sources


# ─── quality enrichment ─────────────
@pytest.mark.smoke
def test_enrich_with_quality_scored(isolated_brain_root):
    """response_quality jsonl の judge スコアが pair に紐付く。"""
    q_file = isolated_brain_root / "clone_improve" / "response_quality" / "2026-05-22.jsonl"
    q_records = [
        {
            "ts": "2026-05-22T10:00:05",
            "judge": {"ai_smell": 4, "mirroring_fit": 5, "length_appropriate": 4, "verdict": "ok"},
        },
    ]
    q_file.write_text("\n".join(json.dumps(r, ensure_ascii=False) for r in q_records), encoding="utf-8")

    from build_fine_tune_dataset import load_quality_index, _enrich_with_quality
    idx = load_quality_index()
    pair = {"user": "q", "assistant": "a", "ts": "2026-05-22T10:00:05", "source": "clone_history"}
    enriched = _enrich_with_quality(pair, idx)
    assert enriched["scored"] is True
    assert enriched["min_quality"] == 4  # min(4, 5, 4)


@pytest.mark.smoke
def test_enrich_with_quality_unscored(isolated_brain_root):
    from build_fine_tune_dataset import _enrich_with_quality
    pair = {"user": "q", "assistant": "a", "ts": "999", "source": "clone_history"}
    enriched = _enrich_with_quality(pair, {})  # empty index
    assert enriched["scored"] is False
    assert enriched["min_quality"] is None


# ─── collect_pairs フィルタ ─────────────
@pytest.mark.smoke
def test_collect_pairs_filters_low_quality(isolated_brain_root):
    """min_quality=3 で 2 以下は除外。"""
    # clone_history pair (scored=2 → 除外)
    user_file = isolated_brain_root / "clone_history" / "u1.jsonl"
    records = [
        {"timestamp": "2026-05-22T10:00:00", "user_id": "u1", "role": "user", "text": "q1"},
        {"timestamp": "2026-05-22T10:00:05", "user_id": "u1", "role": "assistant",
         "text": "ふつうの応答内容 30 字超え" * 2},
    ]
    user_file.write_text("\n".join(json.dumps(r, ensure_ascii=False) for r in records), encoding="utf-8")
    # quality 2 (= 除外対象)
    q_file = isolated_brain_root / "clone_improve" / "response_quality" / "2026-05-22.jsonl"
    q_file.write_text(json.dumps({
        "ts": "2026-05-22T10:00:05",
        "judge": {"ai_smell": 2, "mirroring_fit": 4, "length_appropriate": 4},
    }, ensure_ascii=False), encoding="utf-8")

    from build_fine_tune_dataset import collect_pairs
    pairs = collect_pairs(include_unscored=False, min_quality=3)
    assert len(pairs) == 0  # min_quality=2 で除外


@pytest.mark.smoke
def test_collect_pairs_alignment_always_kept(isolated_brain_root):
    """alignment 系 (= 海山採用 / 直接回答) は quality 採点無しでも採用される。"""
    hist = isolated_brain_root / "alignment_history.json"
    hist.write_text(json.dumps([
        {"question": "Q1", "answer": "海山 answer (30 字超え)" * 2},
    ], ensure_ascii=False), encoding="utf-8")

    from build_fine_tune_dataset import collect_pairs
    pairs = collect_pairs(include_unscored=False, min_quality=3)
    assert len(pairs) == 1
    assert pairs[0]["source"] == "alignment_history"


# ─── OpenAI fine-tune 形式 ─────────────
@pytest.mark.smoke
def test_to_openai_format():
    """OpenAI fine-tune jsonl 形式に正しく変換。"""
    import importlib
    import build_fine_tune_dataset
    importlib.reload(build_fine_tune_dataset)
    from build_fine_tune_dataset import to_openai_format, SYSTEM_PROMPT_FOR_TUNING

    pair = {
        "user": "武蔵小山の今日どう?",
        "assistant": "客数 12 / 売上 38 万 / 単価 31.7K。先週同曜比 -8%。",
        "ts": "2026-05-22T10:00:00",
        "user_id": "u_abc",
        "source": "clone_history",
        "min_quality": 4,
    }
    record = to_openai_format(pair)
    assert "messages" in record
    msgs = record["messages"]
    assert len(msgs) == 3
    assert msgs[0]["role"] == "system"
    assert "うみやまAI" in msgs[0]["content"]
    assert msgs[1]["role"] == "user"
    assert "武蔵小山" in msgs[1]["content"]
    assert msgs[2]["role"] == "assistant"
    assert "38 万" in msgs[2]["content"]
    # metadata 含む
    assert "metadata" in record
    assert record["metadata"]["source"] == "clone_history"
    assert record["metadata"]["min_quality"] == 4


# ─── 集計レポート ─────────────
@pytest.mark.smoke
def test_build_report_handles_empty():
    """0 件でも report 生成できる (= ⚠️ 「fine-tune には少なすぎ」が出る)。"""
    from build_fine_tune_dataset import build_report
    report = build_report([], include_unscored=False, min_quality=3)
    assert "合計: 0 件" in report
    assert "少なすぎ" in report or "0 件" in report


@pytest.mark.smoke
def test_build_report_format():
    """主要 section が含まれている。"""
    from build_fine_tune_dataset import build_report
    pairs = [
        {"user": "q1", "assistant": "a1" * 20, "source": "clone_history", "min_quality": 4},
        {"user": "q2", "assistant": "a2" * 20, "source": "alignment_trial:r1:ok", "min_quality": None},
    ]
    report = build_report(pairs, include_unscored=False, min_quality=3)
    assert "採用件数" in report
    assert "source 別" in report
    assert "quality" in report.lower()
    assert "文字数" in report
