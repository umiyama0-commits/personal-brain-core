"""smoke: OWNDAYS MAGAZINE もぐもぐダイアリー(海山パート)の抽出 (★2026-07-05 海山指示)

要:
1. marker が「目次(■一覧)」と「本文見出し」の 2 箇所に出る時、本文(最長 segment)を採る
   — 従来の indexOf は目次を掴んで本文を落としていた (mogumog_*.md が数百byte の bug)
2. byline (社長 等) を剥がして本文だけ残す
3. 号(magazine id)単位で分割・id で dedup (batch と mogumog の二重取込防止、長い方を残す)
4. 目次だけ/本文なし号は抽出しない (MIN_BODY_CHARS)
LITELLM 非依存 (純粋関数のみ、蒸留 run は含めない)。
"""
from __future__ import annotations

import importlib
import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "scripts"))

MARKER = "海山タケシ社長のもぐもぐダイアリー"
BODY = ("今年もSUMMITが終わった。まずは運営に感謝したい。"
        "一夜明けた朝は、ほっとしたような、少し寂しいような、不思議な余韻が残っていた。"
        "窓際に腰を下ろし、挽きたてのコーヒーをすする。差し込む朝日が昨夜のレーザーライトを思い起こさせる。"
        "嘘である。我が家のどこを探しても、そんな優雅なライフスタイルは存在しない。"
        "現実は、朝からフルスピードで日常に追い回されている。炭酸水をガブ飲みして無理やり目を覚ます。"
        "人生の潔さというのは、案外そういうところにあるのかもしれない。海山でした。")

# 実データ同型: 目次(■一覧) が先、本文(■<marker>) が後。本文の後に別セクション(■他)。
_ISSUE = (
    "OWNDAYS MAGAZINE\n"
    "URL: https://stapa.owndays.net/owndays-magazine-details/108\n"
    "▼ 目次\n"
    f"■{MARKER}\n"          # ← 目次の marker (直後に別項目 = 短い)
    "■見本課長の一喝\n"
    "■編集後記\n"
    "ご挨拶\n本文とは無関係の挨拶。\n"
    f"■{MARKER}\n 社長\n\n{BODY}\n"   # ← 本文の marker (byline + 長文)
    "■見本課長の一喝\n 見本一\n見本さんの記事本文。\n"
)


def _mod():
    import magazine_persona_ingest
    return importlib.reload(magazine_persona_ingest)


@pytest.mark.smoke
def test_longest_segment_picks_body_not_toc():
    m = _mod()
    body = m.longest_diary_segment(_ISSUE)
    assert "嘘である" in body and "海山でした" in body, "本文が取れていない"
    assert "見本課長" not in body, "次セクションを巻き込んでいる"
    assert "社長" not in body.splitlines()[0], "byline が残っている"


@pytest.mark.smoke
def test_extract_columns_associates_id_vol_and_filters():
    m = _mod()
    text = "Vol.356\n" + _ISSUE
    cols = m.extract_diary_columns(text, src_name="onmaga_batch_108-109.md")
    assert len(cols) == 1
    assert cols[0]["id"] == "108"
    assert cols[0]["vol"] == "356"
    assert "嘘である" in cols[0]["body"]


@pytest.mark.smoke
def test_toc_only_issue_yields_nothing():
    """目次に marker はあるが本文が無い号は抽出しない (短い断片を人格に入れない)。"""
    m = _mod()
    toc_only = (
        "URL: https://stapa.owndays.net/owndays-magazine-details/999\n"
        f"▼ 目次\n■{MARKER}\n■編集後記\nご挨拶\n短い挨拶のみ。\n"
    )
    assert m.extract_diary_columns(toc_only) == []


@pytest.mark.smoke
def test_collect_dedups_by_id_keeps_longer(tmp_path):
    m = _mod()
    notes = tmp_path / "notes"
    notes.mkdir()
    # 同 id=108 が batch(本文入り) と mogumog(短い断片) の両方に存在 → 長い方(batch)を残す
    (notes / "onmaga_batch_108-108.md").write_text("Vol.356\n" + _ISSUE, encoding="utf-8")
    (notes / "mogumog_Vol_356_id108.md").write_text(
        "URL: https://stapa.owndays.net/owndays-magazine-details/108\n"
        f"■{MARKER}\n{MARKER}\n見本課長の一喝\n",  # 旧 bug 相当の目次断片
        encoding="utf-8")
    cols = m.collect_all_columns(notes)
    ids = [c["id"] for c in cols]
    assert ids.count("108") == 1, "同 id が二重取込されている"
    assert "嘘である" in [c for c in cols if c["id"] == "108"][0]["body"], "短い方を採ってしまった"


@pytest.mark.smoke
def test_split_issues_multiple_ids():
    m = _mod()
    text = (
        "URL: https://stapa.owndays.net/owndays-magazine-details/108\nA\n"
        "URL: https://stapa.owndays.net/owndays-magazine-details/109\nB\n"
    )
    issues = m._split_issues(text)
    assert [i["id"] for i in issues] == ["108", "109"]


@pytest.mark.smoke
def test_second_issue_keeps_its_vol_header():
    """Reviewer R1: 2号目以降も直前の「## Vol.XXX」ヘッダから Vol を拾える。"""
    m = _mod()
    text = (
        "## Vol.356\nURL: https://stapa.owndays.net/owndays-magazine-details/108\n"
        f"■{MARKER}\n 社長\n\n{BODY}\n■他\n"
        "## Vol.357\nURL: https://stapa.owndays.net/owndays-magazine-details/109\n"
        f"■{MARKER}\n 社長\n\n{BODY}\n■他\n"
    )
    cols = m.extract_diary_columns(text, "batch.md")
    vols = {c["id"]: c["vol"] for c in cols}
    assert vols.get("108") == "356"
    assert vols.get("109") == "357", f"2号目の Vol が取れていない: {vols}"


@pytest.mark.smoke
def test_byline_strip_keeps_short_punchy_opener():
    """Reviewer R2: byline は既知パターンのみ剥がす。短い書き出しは残す。"""
    m = _mod()
    seg = f"\n さて。\n{BODY}"   # 「さて。」は byline でなく書き出し → 残る
    body = m._isolate_body(seg)
    assert body.startswith("さて。"), body
    # 名乗り (社長) は剥がす
    assert m._isolate_body(f"\n 社長\n\n{BODY}").startswith("今年も")


@pytest.mark.smoke
def test_magazine_prompt_authored_by_umiyama_with_guards():
    """★2026-07-05 海山確認 (本人執筆・大半本音): 文体/ユーモア/内省も許可に緩和。
    ただし『他所行きの建前』割り引き・レトリック反転注意・書き言葉 register の注記は必須。"""
    import alignment_interview as ai
    p = ai.MAGAZINE_EXTRACT_PROMPT
    assert "{transcript}" in p
    # 本人執筆前提 + 緩和されたカテゴリ (文体/ユーモア/内省が許可に)
    assert "本人が全て執筆" in p
    for allowed in ("biography", "value_root", "judgment", "philosophy",
                    "humor", "style", "inner_voice"):
        assert allowed in p
    # 残る安全ガード: 建前割り引き / レトリック反転 / 書き言葉 register
    assert "他所行き" in p and "割り引" in p
    assert "嘘である" in p            # レトリック反転の literal 取り違え防止
    assert "書き言葉" in p            # register 混同防止
    assert "high にしない" in p       # 建前は high にしない


@pytest.mark.smoke
def test_extract_session_credit_coverage_false_skips_depth(brain_root, monkeypatch):
    """DA #2: credit_coverage=False は coverage の depth/session_count を加点しない。"""
    import asyncio
    import alignment_interview as ai
    importlib.reload(ai)

    class _Resp:
        def raise_for_status(self): pass
        def json(self):
            return {"choices": [{"message": {"content": json.dumps({
                "items": [{"category": "biography", "insight": "SUMMIT を開催した",
                           "evidence_quote": "SUMMITが終わった", "confidence": "medium"}],
                "dims_with_substance": ["biography"],
                "session_summary": "SUMMIT の話"})}}]}

    class _HTTP:
        async def post(self, *a, **k): return _Resp()

    before = ai.load_coverage()["dimensions"]["biography"]["depth_score"]
    asyncio.run(ai.extract_session("海山: SUMMITが終わった。" * 5, _HTTP(),
                                   "http://x", "k", raw_filename="magazine-1",
                                   credit_coverage=False, source="magazine"))
    after = ai.load_coverage()["dimensions"]["biography"]["depth_score"]
    assert after == before, "credit_coverage=False なのに depth が加点された"
    # source タグが保存 json に付く
    ext = list((brain_root / "alignment" / "interview_extracted").glob("magazine-1.json"))
    assert ext, "抽出 json が保存されていない"
    assert json.loads(ext[0].read_text(encoding="utf-8")).get("source") == "magazine"


@pytest.mark.smoke
def test_real_batch_files_extract_umiyama_diary():
    """本番 raw の実バッチから海山 diary が取れる (回帰: 本文入り抽出の証明)。"""
    m = _mod()
    notes = ROOT / "data" / "brain" / "raw" / "notes"
    if not list(notes.glob("onmaga_batch_*.md")):
        pytest.skip("onmaga_batch raw が無い環境")
    cols = m.collect_all_columns(notes)
    assert cols, "実バッチから diary が 1 件も取れない"
    for c in cols:
        assert len(c["body"]) >= m.MIN_BODY_CHARS
        # 目次断片でなく本文 (句点を複数含むプロパー文章)
        assert c["body"].count("。") >= 3, f"id{c['id']} が本文でなく断片の疑い"
