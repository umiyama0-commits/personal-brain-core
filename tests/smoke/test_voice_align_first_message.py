"""smoke: 音声アラインメント 冒頭バリエーション + wiki 話のタネ連携 (★2026-07-04 海山指示)

1. 冒頭 firstMessage が画一的でない (時間帯 / 間隔 / 話のタネで毎回変わる)
2. 口調 style ルール (うん/はい NG、です/ます堅苦しい定型 NG) を冒頭でも守る
3. collect_wiki_topics が最近更新の wiki だけ拾い、除外対象 (personal/dev / 売上
   自動生成 / 古い file) を混ぜない
4. build_interviewer_system_prompt に話のタネ section + 捏造ガードが入る
5. main.py の配線 (build_first_message / collect_wiki_topics) が消えない
"""
from __future__ import annotations

import importlib
import os
import random
import time
from datetime import datetime, timezone, timedelta
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent.parent
JST = timezone(timedelta(hours=9))


def _ai():
    import alignment_interview
    return importlib.reload(alignment_interview)


# ── 1. 冒頭バリエーション ──────────────────────────────


@pytest.mark.smoke
def test_first_message_varies_across_calls():
    """同条件でも rng で毎回違う冒頭になる (画一的の根治)。"""
    ai = _ai()
    now = datetime(2026, 7, 4, 9, 0, tzinfo=JST)
    msgs = {
        ai.build_first_message(now=now, rng=random.Random(i))
        for i in range(40)
    }
    assert len(msgs) >= 5, f"バリエーション不足: {msgs}"


@pytest.mark.smoke
def test_first_message_respects_time_slot():
    """朝は朝の挨拶、夜は夜の挨拶 (時間帯でトーンが変わる)。"""
    ai = _ai()
    for i in range(20):
        m = ai.build_first_message(
            now=datetime(2026, 7, 4, 7, 30, tzinfo=JST), rng=random.Random(i))
        assert ("おはよう" in m) or ("朝" in m), m
        m = ai.build_first_message(
            now=datetime(2026, 7, 4, 23, 30, tzinfo=JST), rng=random.Random(i))
        assert ("こんな時間" in m) or ("夜遅く" in m), m


@pytest.mark.smoke
def test_first_message_long_gap_leadin():
    """前回から 14 日以上空くと「久しぶり」系を必ず挟む。"""
    ai = _ai()
    now = datetime(2026, 7, 4, 12, 0, tzinfo=JST)
    old = (now - timedelta(days=30)).isoformat(timespec="seconds")
    for i in range(20):
        m = ai.build_first_message(
            last_session_ts=old, now=now, rng=random.Random(i))
        assert ("久しぶり" in m) or ("しばらくぶり" in m), m
    # 直近 (2 日前) なら挟まない
    recent = (now - timedelta(days=2)).isoformat(timespec="seconds")
    for i in range(20):
        m = ai.build_first_message(
            last_session_ts=recent, now=now, rng=random.Random(i))
        assert "久しぶり" not in m and "しばらくぶり" not in m, m


@pytest.mark.smoke
def test_first_message_topic_hook_sometimes():
    """話のタネがあれば時々「そういえば○○」で始まる (毎回ではない)。"""
    ai = _ai()
    now = datetime(2026, 7, 4, 12, 0, tzinfo=JST)
    msgs = [
        ai.build_first_message(
            topic_hints=["Example Garden"], now=now, rng=random.Random(i))
        for i in range(60)
    ]
    with_topic = [m for m in msgs if "Example Garden" in m]
    assert with_topic, "topic hook が一度も出ない"
    assert len(with_topic) < len(msgs), "topic hook が毎回出る (画一化の逆戻り)"
    # 不正な hint (長すぎ / 空 / 改行 / 日付始まり / markdown 記法) は無視され例外にならない
    bad = ["", "x" * 60, "a\nb", "2026-07-01-shop-meeting", "**太字** タイトル"]
    for i in range(30):
        m = ai.build_first_message(topic_hints=bad, now=now, rng=random.Random(i))
        assert m and "x" * 30 not in m and "\n" not in m
        assert "2026-" not in m and "**" not in m, m


@pytest.mark.smoke
def test_first_message_style_rules():
    """冒頭も style ルール準拠: うん/はい 始まり NG、堅い定型 NG、音声向けに短い。"""
    ai = _ai()
    for hour in (7, 12, 19, 23):
        now = datetime(2026, 7, 4, hour, 0, tzinfo=JST)
        for i in range(30):
            m = ai.build_first_message(
                topic_hints=["店舗の件"], last_summary="前回メモ",
                now=now, rng=random.Random(i))
            assert not m.startswith(("うん", "はい")), m
            assert "お聞かせ" not in m and "ください" not in m, m
            assert "お疲れ様" not in m, f"お疲れさま はひらがな: {m}"
            assert len(m) <= 80, f"音声冒頭には長すぎ ({len(m)}字): {m}"


# ── 2. wiki 話のタネ ──────────────────────────────


def _seed_topic_wiki(brain_root) -> Path:
    wiki = brain_root / "wiki"
    (wiki / "meetings").mkdir(parents=True, exist_ok=True)
    (wiki / "personal" / "example-garden").mkdir(parents=True, exist_ok=True)
    (wiki / "personal" / "dev").mkdir(parents=True, exist_ok=True)
    (wiki / "knowledge").mkdir(parents=True, exist_ok=True)
    (wiki / "knowledge" / "history").mkdir(parents=True, exist_ok=True)
    today = datetime.now().strftime("%Y-%m-%d")
    (wiki / "meetings" / f"{today}-shop-meeting.md").write_text(
        "---\nclone_visibility: private\n---\n# 店舗展開 定例\n"
        "台湾出店の議論。候補は 2 箇所。", encoding="utf-8")
    (wiki / "personal" / "example-garden" / "note.md").write_text(
        "# Example Garden 進捗\n温室の資材選定を開始。", encoding="utf-8")
    # 除外対象: 開発ログ / 売上自動生成 (★実運用の file 名 = cross-check Reviewer 指摘) /
    # history/ subtree / 古い file / mtime だけ新しく中身が古い file
    (wiki / "personal" / "dev" / "log.md").write_text(
        "# dev journal\nDEVLOGMARK", encoding="utf-8")
    for fn in ("owndays-daily-sales.md", "owndays-history-nationdaily.md",
               "owndays-monday-dash-latest.md", "owndays-store-master.md",
               "owndays-am-sv-summary.md"):
        (wiki / "knowledge" / fn).write_text(
            f"# 自動生成 {fn}\nSALESMARK 20M", encoding="utf-8")
    (wiki / "knowledge" / "history" / "deep.md").write_text(
        "# 履歴\nHISTMARK", encoding="utf-8")
    old = wiki / "knowledge" / "old-note.md"
    old.write_text("# 古いメモ\nOLDMARK", encoding="utf-8")
    stale = time.time() - 60 * 86400
    os.utime(old, (stale, stale))
    # 再 clone 相当: mtime は今、file 名の日付は 60 日前 (偽鮮度)
    fake_date = (datetime.now() - timedelta(days=60)).strftime("%Y-%m-%d")
    (wiki / "meetings" / f"{fake_date}-old-meeting.md").write_text(
        "# 昔の会議\nFAKEFRESHMARK", encoding="utf-8")
    return wiki


@pytest.mark.smoke
def test_collect_wiki_topics_recent_only(brain_root):
    ai = _ai()
    _seed_topic_wiki(brain_root)
    topics = ai.collect_wiki_topics(max_items=8, days=21)
    titles = [t["title"] for t in topics]
    blob = str(topics)
    assert "店舗展開 定例" in titles
    assert "Example Garden 進捗" in titles
    assert "DEVLOGMARK" not in blob and "dev journal" not in titles, \
        "personal/dev (開発ログ) が話のタネに混入"
    assert "SALESMARK" not in blob, "売上自動生成 file が話のタネに混入"
    assert "HISTMARK" not in blob, "knowledge/history/ が話のタネに混入"
    assert "OLDMARK" not in blob, "鮮度切れ file が話のタネに混入"
    assert "FAKEFRESHMARK" not in blob, \
        "mtime だけ新しい古 file (再 clone 偽鮮度) が話のタネに混入"
    # frontmatter は excerpt に混ぜない
    assert "clone_visibility" not in blob


@pytest.mark.smoke
def test_topic_title_cleaned_for_speech(brain_root):
    """title の markdown 記法 / 日付 prefix は除去 (TTS がそのまま読むため)。"""
    ai = _ai()
    wiki = brain_root / "wiki"
    (wiki / "decisions").mkdir(parents=True, exist_ok=True)
    today = datetime.now().strftime("%Y-%m-%d")
    (wiki / "decisions" / "noisy.md").write_text(
        f"# {today} **台湾** [[plan|出店]] `検討`\n本文。", encoding="utf-8")
    topics = ai.collect_wiki_topics()
    titles = [t["title"] for t in topics]
    assert "台湾 出店 検討" in titles, titles


@pytest.mark.smoke
def test_format_and_prompt_injection(brain_root):
    ai = _ai()
    _seed_topic_wiki(brain_root)
    block = ai.format_wiki_topics(ai.collect_wiki_topics())
    assert block.startswith("- [")
    prompt = ai.build_interviewer_system_prompt(wiki_topics=block)
    assert "話のタネ" in prompt
    assert "捏造禁止" in prompt, "話のタネ section に捏造ガードが無い"
    assert "店舗展開 定例" in prompt
    # 空なら section ごと出さない
    prompt_empty = ai.build_interviewer_system_prompt(wiki_topics="")
    assert "話のタネ" not in prompt_empty


@pytest.mark.smoke
def test_latest_session_summary(brain_root, sample_alignment_extracted):
    ai = _ai()
    assert "孤独感" in ai.latest_session_summary()


@pytest.mark.smoke
def test_latest_session_summary_skips_rejected(brain_root, sample_alignment_extracted):
    """却下済セッションの summary は「この前の続き」に使わない (cross-check DA)。"""
    import json
    ai = _ai()
    edir = brain_root / "alignment" / "interview_extracted"
    f = edir / f"{sample_alignment_extracted}.json"
    d = json.loads(f.read_text(encoding="utf-8"))
    d["status"] = "rejected"
    f.write_text(json.dumps(d, ensure_ascii=False), encoding="utf-8")
    assert ai.latest_session_summary() == ""


@pytest.mark.smoke
def test_latest_session_summary_empty(brain_root):
    ai = _ai()
    assert ai.latest_session_summary() == ""


# ── 3. main.py 配線の固定 (source level、logging test と同型) ──────────


@pytest.mark.smoke
def test_main_wires_first_message_and_topics():
    src = (REPO / "main.py").read_text(encoding="utf-8")
    i = src.find("async def _build_voice_align_assistant_config")
    assert i > 0
    body = src[i:i + 9000]
    assert "build_first_message" in body, "動的 firstMessage の配線が消えた"
    assert "collect_wiki_topics" in body, "wiki 話のタネの配線が消えた"
    assert "build_interviewer_system_prompt" in body
    # 冒頭で発話してよい topic hint は hobbies / personal のみ (cross-check DA:
    # meetings / knowledge の題名を、誰が聞いてるか不明な冒頭で読み上げない)
    assert '("hobbies", "personal")' in body, "冒頭 topic hint の dir 制限が消えた"
    # web-config が固定文言を渡していない (画一的 opener の復活防止)
    j = src.find("async def voice_align_web_config")
    assert j > 0
    wbody = src[j:j + 1600]
    assert "first_message=(" not in wbody, "web 経路に固定 firstMessage が復活"


@pytest.mark.smoke
def test_main_caller_allowlist_wiring():
    """VOICE_ALIGN_CALLER_ALLOWLIST (opt-in 発信者検証) の配線固定 (cross-check DA):
    assistant-request = 縮退 config、end-of-call = 蒸留 skip の両方で照合すること。"""
    src = (REPO / "main.py").read_text(encoding="utf-8")
    assert "VOICE_ALIGN_CALLER_ALLOWLIST" in src
    i = src.find("async def voice_alignment_webhook")
    assert i > 0
    fn_end = src.find("\nasync def _process_voice_alignment", i)
    body = src[i:fn_end]
    assert body.count("_voice_align_caller_trusted(") >= 2, \
        "発信者検証が assistant-request / end-of-call の両方に無い"
    # extract-pending が隔離 raw (*-untrusted.md) を再蒸留しない
    api_src = (REPO / "routes" / "brain_api.py").read_text(encoding="utf-8")
    assert '-untrusted' in api_src, \
        "extract-pending の untrusted raw 除外が消えた (蒸留 skip の迂回路になる)"


@pytest.mark.smoke
def test_collect_wiki_topics_callsite_pinned():
    """§1.17 規律①の意図的例外 pin (cross-check Reviewer): collect_wiki_topics は
    personal/ を含む 海山専用 reader。OWNDAYS-facing 経路への転用を防ぐため、
    呼び出しを voice-align (main._build_voice_align_assistant_config) の 1 箇所に固定。
    正当に増やす時はこの test と docs/integrations/vapi-voice-alignment.md を同時更新。"""
    callers = []
    for p in list(REPO.glob("*.py")) + list(REPO.glob("routes/*.py")) \
            + list(REPO.glob("scripts/**/*.py")) + list(REPO.glob("tasks/*.py")) \
            + list(REPO.glob("services/*.py")) + list(REPO.glob("brain_wiki_helpers/*.py")):
        if p.name == "alignment_interview.py":
            continue
        src = p.read_text(encoding="utf-8")
        n = sum(1 for ln in src.splitlines()
                if "collect_wiki_topics(" in ln and not ln.strip().startswith("#"))
        if n:
            callers.append((str(p.relative_to(REPO)), n))
    assert callers == [("main.py", 1)], \
        f"collect_wiki_topics の呼び出し箇所が想定外: {callers}"
