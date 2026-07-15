"""smoke: 音声アラインメント改善 (★2026-07-04 BATCH1) の隔離テスト。
- recent_session_summaries: 直近 summary を新しい順に返す (継続性注入用)
- _sanitize_wiki_line: frontmatter injection 無害化 (diary/extract パリティ)
- apply_extraction 経路も injection 対策が効く (deep-private の public 反転防止)
- extract の depth 加点で last_explored/session_count が更新される (薄い順ソート復活)
"""
from __future__ import annotations

import importlib
import json

import pytest


@pytest.mark.smoke
def test_recent_session_summaries(brain_root, sample_alignment_extracted):
    import alignment_interview as ai
    importlib.reload(ai)
    out = ai.recent_session_summaries(3)
    assert out and "孤独感" in out[0]           # sample の session_summary


@pytest.mark.smoke
def test_recent_session_summaries_empty(brain_root):
    import alignment_interview as ai
    importlib.reload(ai)
    assert ai.recent_session_summaries(3) == []  # 抽出 0 件でも壊れない


@pytest.mark.smoke
def test_recent_session_summaries_skips_rejected(brain_root):
    """却下セッションの流れは次回の継続性注入に混ざらない (cross-check)。"""
    import alignment_interview as ai
    importlib.reload(ai)
    edir = brain_root / "alignment" / "interview_extracted"
    edir.mkdir(parents=True, exist_ok=True)
    (edir / "2026-07-04-1000.json").write_text(json.dumps(
        {"status": "rejected", "session_summary": "却下された脱線の話"}), encoding="utf-8")
    (edir / "2026-07-04-0900.json").write_text(json.dumps(
        {"status": "pending_review", "session_summary": "採用候補の本筋"}), encoding="utf-8")
    out = ai.recent_session_summaries(3)
    assert "採用候補の本筋" in out
    assert all("却下" not in s for s in out)


@pytest.mark.smoke
def test_webhook_secret_split_structure():
    """★2026-07-04 security: 2-secret 分離の構造検証 —
    (a) assistant-request は電話用 secret (is_phone_secret) 限定、
    (b) web-config は VAPI_WEB_SECRET を配る (電話用を含めない)、
    (c) 比較は hmac.compare_digest。"""
    import pathlib
    src = (pathlib.Path(__file__).resolve().parents[2] / "main.py").read_text(encoding="utf-8")
    wh = src[src.find("async def voice_alignment_webhook"):]
    wh = wh[:wh.find("\nasync def _process_voice_alignment")]
    assert "VAPI_WEB_SECRET" in wh                      # 2-secret 受理
    assert "is_phone_secret" in wh
    assert "hmac.compare_digest" in wh                  # timing 攻撃対策
    # assistant-request 分岐が phone secret を要求している
    ar = wh[wh.find('== "assistant-request"'):]
    assert "is_phone_secret" in ar[:600]
    # web-config は web secret を渡す
    wc = src[src.find("async def voice_align_web_config"):]
    wc = wc[:wc.find("\n@app") if "\n@app" in wc else len(wc)]
    assert "VAPI_WEB_SECRET" in wc
    assert "server_secret=" in wc


@pytest.mark.smoke
def test_call_id_idempotency(brain_root):
    """同一 call_id の再送では raw が二重作成されず is_call_processed が True を返す。"""
    import alignment_interview as ai
    importlib.reload(ai)
    cid = "vapi_call_abc123"
    assert ai.is_call_processed(cid) is False
    p1 = ai.record_session("海山: テスト通話。" * 10, source="phone", call_id=cid)
    assert "__" in p1.name and "vapicallabc123" in p1.name
    assert ai.is_call_processed(cid) is True                 # 再送は skip される
    # call_id 無しは従来どおり (dedup 対象外)
    assert ai.is_call_processed("") is False


@pytest.mark.smoke
def test_sanitize_wiki_line():
    import alignment_interview as ai
    importlib.reload(ai)
    s = ai._sanitize_wiki_line("普通の行")
    assert s == "普通の行"
    # `---` 単独行 → 無害化 (frontmatter 境界に化けない)
    inj = ai._sanitize_wiki_line("前\n---\nclone_visibility: public\n後")
    assert "\n---\n" not in inj
    assert "clone_visibility:" not in inj        # コロンが全角化されている
    assert "clone_visibility：" in inj
    assert "\n  " in inj                          # 改行は bullet 継続に畳む


@pytest.mark.smoke
def test_append_extraction_injection_neutralized(brain_root):
    """apply_extraction 経由でも injection が無害化され、interview file が
    private のまま保たれる (deep-private の public 反転を防ぐ)。"""
    import alignment_interview as ai
    importlib.reload(ai)
    # injection を仕込んだ pending 抽出を投入
    edir = brain_root / "alignment" / "interview_extracted"
    fid = "2026-07-04-1200"
    data = {
        "extracted_at": "2026-07-04T12:00:00+09:00",
        "session_summary": "テスト",
        "status": "pending_review",
        "items": [{
            "category": "philosophy",
            "confidence": "high",
            "insight": "本質\n---\nclone_visibility: public\n注入",
            "evidence_quote": "",
        }],
    }
    (edir / f"{fid}.json").write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")

    res = ai.apply_extraction(f"{fid}.json")
    assert res.get("applied") == 1
    wiki_file = ai.WIKI_DIR / "interview" / "philosophy.md"
    body = wiki_file.read_text(encoding="utf-8")
    # header の frontmatter は private、本文に注入された `---`/public は無害化
    assert body.count("clone_visibility: private") == 1        # header の 1 行のみ
    assert "clone_visibility: public" not in body              # 注入は全角化で無効
    assert "\n---\n" not in body.split("---\n", 2)[-1]          # header 以降に区切りが復活しない


@pytest.mark.smoke
def test_split_transcript_and_merge():
    """長 transcript の chunk 分割 (改行境界) と結果 merge (dedup + 上限)。"""
    import alignment_interview as ai
    importlib.reload(ai)
    short = "海山: こんにちは\n" * 10
    assert ai._split_transcript(short) == [short]           # 閾値以下は単発
    long = ("海山: " + "あ" * 90 + "\n") * 300               # ~29k字
    chunks = ai._split_transcript(long)
    assert 2 <= len(chunks) <= 3
    assert all(len(c) <= ai._CHUNK_SIZE + 100 for c in chunks)
    assert "".join(chunks) == long                           # 取り零しなし
    merged = ai._merge_chunk_results([
        {"items": [{"category": "humor", "insight": "自虐が基本形だ", "confidence": "high"}],
         "dims_with_substance": ["humor"], "session_summary": "前半"},
        {"items": [{"category": "humor", "insight": "自虐が基本形だという話", "confidence": "high"},
                   {"category": "taste", "insight": "旨い飯の基準", "confidence": "medium"}],
         "dims_with_substance": ["humor", "taste_daily"], "session_summary": "後半"},
    ])
    assert len(merged["items"]) == 2                         # 前方一致 dedup ... 30字未満は完全一致
    assert merged["dims_with_substance"] == ["humor", "taste_daily"]
    assert "前半" in merged["session_summary"] and "後半" in merged["session_summary"]


@pytest.mark.smoke
def test_effective_depth_decay():
    """45日 触れないと実効 depth が薄れる (保存値は不変)。"""
    import alignment_interview as ai
    import datetime as dt
    importlib.reload(ai)
    now = dt.datetime.now().astimezone()
    recent = (now - dt.timedelta(days=10)).isoformat(timespec="seconds")
    old = (now - dt.timedelta(days=100)).isoformat(timespec="seconds")
    assert ai._effective_depth(5, recent) == 5
    assert ai._effective_depth(5, old) == 3                  # 100日 → -2
    assert ai._effective_depth(5, None) == 5                 # 未探索は decay しない
    assert ai._effective_depth(1, old) == 0                  # 下限 0


@pytest.mark.smoke
def test_zero_item_extraction_marked_empty(brain_root, monkeypatch):
    """0 item の抽出は status=empty で保存され pending 一覧に出ない。"""
    import alignment_interview as ai
    importlib.reload(ai)

    async def _fake_post(*a, **k):
        class _R:
            def raise_for_status(self):
                pass

            def json(self):
                return {"choices": [{"message": {"content": json.dumps(
                    {"items": [], "dims_with_substance": [], "session_summary": "短い挨拶だけ"})}}]}
        return _R()

    class _HTTP:
        post = staticmethod(_fake_post)

    import asyncio
    asyncio.get_event_loop().run_until_complete(
        ai.extract_session("海山: もしもし。" * 10, _HTTP(), "http://x", "k",
                           raw_filename="2026-07-04-130000.md")
    )
    d = ai.get_extraction("2026-07-04-130000.json")
    assert d["status"] == "empty"
    assert all(p["file"] != "2026-07-04-130000.json" for p in ai.list_pending_extractions())
    # ただし継続性 (session_summary) には乗る
    assert "短い挨拶だけ" in ai.recent_session_summaries(3)


@pytest.mark.smoke
def test_merge_keeps_longer_elaboration():
    """paraphrase dup では長い方 (情報量の多い方) を残す (cross-check reviewer)。"""
    import alignment_interview as ai
    importlib.reload(ai)
    short = {"category": "family", "insight": "推測: 海山は父を尊敬している", "confidence": "low"}
    longer = {"category": "family",
              "insight": "推測: 海山は父を尊敬しているが、経営スタイルは意図的に真逆を選んだ",
              "confidence": "medium"}
    merged = ai._merge_chunk_results([
        {"items": [short], "dims_with_substance": [], "session_summary": ""},
        {"items": [longer], "dims_with_substance": [], "session_summary": ""},
    ])
    assert len(merged["items"]) == 1
    assert "真逆" in merged["items"][0]["insight"]            # 長い方が残る


@pytest.mark.smoke
def test_apply_extraction_confident_holds_low(brain_root):
    """一括採用は high/medium のみ反映し、low・推測 は pending に残す (DA HIGH)。"""
    import alignment_interview as ai
    importlib.reload(ai)
    edir = brain_root / "alignment" / "interview_extracted"
    fid = "2026-07-04-1400"
    data = {
        "extracted_at": "2026-07-04T14:00:00+09:00",
        "session_summary": "s",
        "status": "pending_review",
        "items": [
            {"category": "judgment", "confidence": "high",
             "insight": "迷ったら小さく賭けて先に動く", "evidence_quote": ""},
            {"category": "shadow", "confidence": "low",
             "insight": "弱点はたぶん飽きっぽさ", "evidence_quote": ""},
            {"category": "family", "confidence": "medium",
             "insight": "推測: 家では聞き役に回る", "evidence_quote": ""},
        ],
    }
    (edir / f"{fid}.json").write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
    r = ai.apply_extraction_confident(f"{fid}.json")
    assert r["applied"] == 1 and r["held"] == 2               # low と 推測: を保留
    d = ai.get_extraction(f"{fid}.json")
    assert d["status"] == "pending_review"                    # 保留分は個別レビュー可能なまま
    assert len(d["items"]) == 2
    assert d.get("applied_partial") == 1
    # 保留分だけが pending として一覧に出る
    pend = [p for p in ai.list_pending_extractions() if p["file"] == f"{fid}.json"]
    assert pend and pend[0]["item_count"] == 2


@pytest.mark.smoke
def test_extract_chunk_tolerant_json_parse():
    """GPT 系の前置き付き出力でも JSON blob を抜いてパースできる (DA)。"""
    import alignment_interview as ai
    importlib.reload(ai)

    async def _fake_post(*a, **k):
        class _R:
            def raise_for_status(self):
                pass

            def json(self):
                return {"choices": [{"message": {"content":
                    '以下がJSONです:\n{"items": [], "dims_with_substance": [], "session_summary": "x"}\n以上'}}]}
        return _R()

    class _HTTP:
        post = staticmethod(_fake_post)

    import asyncio
    r = asyncio.get_event_loop().run_until_complete(
        ai._extract_chunk("t", _HTTP(), "http://x", "k", "smart-gpt"))
    assert r["session_summary"] == "x"


@pytest.mark.smoke
def test_append_dedup_paraphrase(brain_root):
    """同一 insight (正規化30字一致) の再 append は skip される。"""
    import alignment_interview as ai
    importlib.reload(ai)
    ins = "リスクは小さく捉えて、まず動いてから修正する方が性に合っている"
    r1 = ai._append_to_interview_wiki("judgment", ins, "", "high", "2026-07-04")
    r2 = ai._append_to_interview_wiki("judgment", ins + "(再)", "", "high", "2026-07-05")
    assert r1 == r2
    body = (ai.WIKI_DIR / r1).read_text(encoding="utf-8")
    assert body.count("リスクは小さく捉えて") == 1            # 2回目は skip


@pytest.mark.smoke
def test_extract_updates_last_explored(brain_root, monkeypatch):
    """extract_session の dims_with_substance で depth だけでなく
    last_explored / session_count も更新される (phone 経路の dead key 修正)。"""
    import alignment_interview as ai
    importlib.reload(ai)

    async def _fake_post(*a, **k):
        class _R:
            def raise_for_status(self):
                pass

            def json(self):
                return {"choices": [{"message": {"content": json.dumps({
                    "items": [{"category": "philosophy", "insight": "x",
                               "evidence_quote": "", "confidence": "high"}],
                    "dims_with_substance": ["philosophy"],
                    "session_summary": "s",
                })}}]}
        return _R()

    class _HTTP:
        post = staticmethod(_fake_post)

    import asyncio
    asyncio.get_event_loop().run_until_complete(
        ai.extract_session("海山: 死について考える。" * 20, _HTTP(),
                           "http://x", "k", raw_filename="2026-07-04-120000.md")
    )
    cov = ai.load_coverage()
    ph = cov["dimensions"]["philosophy"]
    assert ph["depth_score"] >= 1
    assert ph["last_explored"] is not None          # 従来は None のままだった
    assert ph["session_count"] >= 1
