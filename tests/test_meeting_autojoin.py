"""tests/test_meeting_autojoin.py — ★2026-07-03 web会議 自動参加の参加判定テスト。

should_join は「録音してはいけない会議に bot を送らない」防御線 (meeting-pipeline.md の
録音除外 policy)。fail-safe (迷ったら skip) を回帰で固定する。
"""
from __future__ import annotations

import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))

from meeting_autojoin import (  # noqa: E402
    build_transcript_text,
    extract_meeting_url,
    should_join,
)

JST = timezone(timedelta(hours=9))
NOW = datetime(2026, 7, 3, 12, 0, tzinfo=JST)


def _ev(summary="経営定例", start_h=2.0, dur_min=60, attendees=None,
        hangout="https://meet.google.com/abc-defg-hij", **kw):
    start = NOW + timedelta(hours=start_h)
    ev = {
        "id": "ev1", "status": "confirmed", "summary": summary,
        "organizer": {"email": "ceo@owndays.co.jp"},
        "start": {"dateTime": start.isoformat()},
        "end": {"dateTime": (start + timedelta(minutes=dur_min)).isoformat()},
        # ★DA R1: 2人会議 = de-facto 1on1 で skip になるため default は 3人会議
        "attendees": attendees if attendees is not None else [
            {"email": "ceo@owndays.co.jp", "self": True,
             "responseStatus": "accepted"},
            {"email": "tanaka@owndays.co.jp", "responseStatus": "accepted"},
            {"email": "suzuki@owndays.co.jp", "responseStatus": "accepted"},
        ],
    }
    if hangout:
        ev["hangoutLink"] = hangout
    ev.update(kw)
    return ev


def test_normal_internal_meeting_joins():
    ok, reason = should_join(_ev(), NOW)
    assert ok, reason


def test_denylist_blocks_sensitive_meetings():
    # 実カレンダーで実在した「CFO面接」含む録音除外 policy の代表例
    for title in ["CFO面接　Akikoさん", "人事評価 会議", "田中さん 1on1",
                  "M&A 検討", "弁護士 打合せ", "健康診断 結果説明",
                  "ハラスメント相談窓口 定例", "営業定例 [no-ai]"]:
        ok, reason = should_join(_ev(summary=title), NOW)
        assert not ok and reason in ("denylist",), (title, reason)


def test_ai_ok_marker_overrides_denylist():
    ok, reason = should_join(_ev(summary="評価制度の設計レビュー [ai-ok]"), NOW)
    assert ok, reason


def test_external_attendee_blocked_by_default():
    ev = _ev(attendees=[
        {"email": "ceo@owndays.co.jp", "self": True, "responseStatus": "accepted"},
        {"email": "tanaka@owndays.co.jp", "responseStatus": "accepted"},
        {"email": "partner@example.com", "responseStatus": "accepted"},
    ])
    ok, reason = should_join(ev, NOW)
    assert not ok and reason.startswith("external:")
    ok2, _ = should_join(ev, NOW, allow_external=True)
    assert ok2


def test_owndays_global_domain_is_internal():
    # dry-run 実測: owndays.com (グローバル側) の同僚は社内扱い
    ev = _ev(attendees=[
        {"email": "ceo@owndays.co.jp", "self": True, "responseStatus": "accepted"},
        {"email": "member2@owndays.com", "responseStatus": "accepted"},
        {"email": "tanaka@owndays.co.jp", "responseStatus": "accepted"},
    ])
    ok, reason = should_join(ev, NOW)
    assert ok, reason
    # 親会社 lenskart.com は default 非許可 (env で解放可能)
    ev2 = _ev(attendees=[
        {"email": "ceo@owndays.co.jp", "self": True, "responseStatus": "accepted"},
        {"email": "tanaka@owndays.co.jp", "responseStatus": "accepted"},
        {"email": "partner1@lenskart.com", "responseStatus": "accepted"},
    ])
    assert should_join(ev2, NOW)[1] == "external:lenskart.com"


def test_declined_all_day_ended_solo_and_no_url_skipped():
    assert should_join(_ev(attendees=[
        {"email": "ceo@owndays.co.jp", "self": True, "responseStatus": "declined"},
        {"email": "tanaka@owndays.co.jp"}]), NOW)[1] == "declined"
    all_day = _ev()
    all_day["start"] = {"date": "2026-07-03"}
    assert should_join(all_day, NOW)[1] == "all_day"
    assert should_join(_ev(start_h=-3, dur_min=30), NOW)[1] == "ended"
    assert should_join(_ev(attendees=[
        {"email": "ceo@owndays.co.jp", "self": True}]), NOW)[1] == "solo"
    assert should_join(_ev(hangout=None), NOW)[1] == "no_video_url"
    assert should_join(_ev(dur_min=300), NOW)[1] == "too_long"


def test_two_person_meeting_skipped_as_de_facto_1on1():
    """★DA R1 (実カレンダー実証): 「Catchup」「X<>Y」型の英語命名 1on1 は regex で
    追えない → 2人会議は人数で skip。[ai-ok] で opt-in 可。"""
    two = [
        {"email": "ceo@owndays.co.jp", "self": True, "responseStatus": "needsAction"},
        {"email": "colleague1@owndays.co.jp", "responseStatus": "accepted"},
    ]
    ok, reason = should_join(_ev(summary="Weekly Catchup: Andy <> Umiyama-San",
                                 attendees=two), NOW)
    assert not ok and reason == "two_person"
    ok2, _ = should_join(_ev(summary="Weekly Catchup [ai-ok]", attendees=two), NOW)
    assert ok2


def test_denylist_v2_additions():
    """★DA R2 + reviewer SF-4: 個別処遇・雇用・機密系の拡張分。"""
    for title in ["給与改定 会議", "役員報酬 検討", "退職の件", "昇進会議",
                  "Comp Review", "Performance Review Q2", "Offer discussion",
                  "組織再編（極秘）", "1:1 田中さん", "内部監査 定例"]:
        ok, reason = should_join(_ev(summary=title), NOW)
        assert not ok and reason == "denylist", (title, reason)
    # 販促系の誤 block はしない (bare promotion/performance は対象外)
    for title in ["夏のプロモーション定例", "MKカレンダー Update&Planning",
                  "Performance marketing 定例"]:
        ok, reason = should_join(_ev(summary=title), NOW)
        assert ok, (title, reason)


def test_ai_ok_requires_internal_organizer():
    """★DA R5: 社外 organizer が [ai-ok] を書いても bot を引き込めない。"""
    ev = _ev(summary="打合せ [ai-ok]", attendees=[
        {"email": "ceo@owndays.co.jp", "self": True},
        {"email": "tanaka@owndays.co.jp"},
        {"email": "sales@example.com"},
    ])
    ev["organizer"] = {"email": "sales@example.com"}
    ok, reason = should_join(ev, NOW)
    assert not ok and reason.startswith("external:")
    # 社内 organizer なら [ai-ok] は有効
    ev["organizer"] = {"email": "ceo@owndays.co.jp"}
    assert should_join(ev, NOW)[0] is True


def test_no_attendees_and_z_suffix():
    ev = _ev(attendees=[])
    assert should_join(ev, NOW)[1] == "no_attendees"
    # Z-suffix dateTime は host py3.9 でも落ちない (_parse_dt)。
    # 06:00Z = 15:00 JST (NOW=12:00 JST の未来) → 正常に join 判定される
    evz = _ev()
    evz["start"] = {"dateTime": "2026-07-03T06:00:00Z"}
    evz["end"] = {"dateTime": "2026-07-03T07:00:00Z"}
    ok, reason = should_join(evz, NOW)
    assert ok, reason


def test_extra_denylist_and_broken_regex():
    ev = _ev(summary="Project Zeta 進捗")
    assert should_join(ev, NOW, extra_deny="zeta")[1] == "denylist_extra"
    # 壊れた extra regex は無視 (基本 denylist は別途適用済)
    assert should_join(ev, NOW, extra_deny="([")[0] is True


def test_extract_meeting_url_priority_and_regex():
    assert extract_meeting_url(_ev()) == "https://meet.google.com/abc-defg-hij"
    ev = _ev(hangout=None)
    ev["conferenceData"] = {"entryPoints": [
        {"entryPointType": "video", "uri": "https://meet.google.com/xyz-conf-uri"}]}
    assert extract_meeting_url(ev).endswith("xyz-conf-uri")
    ev2 = _ev(hangout=None)
    ev2["description"] = "参加: https://us02web.zoom.us/j/123456789?pwd=abc"
    assert "zoom.us/j/123456789" in extract_meeting_url(ev2)
    assert extract_meeting_url(_ev(hangout=None)) == ""


def test_build_transcript_text_both_shapes():
    legacy = [{"speaker": "海山", "words": [{"text": "おはよう"}, {"text": "始めよう"}]},
              {"speaker": "田中", "words": [{"text": "了解です"}]}]
    text, speakers = build_transcript_text(legacy)
    assert "海山: おはよう 始めよう" in text and speakers == ["海山", "田中"]
    new = [{"participant": {"name": "海山"}, "words": [{"text": "次の議題"}]}]
    text2, sp2 = build_transcript_text(new)
    assert text2 == "海山: 次の議題" and sp2 == ["海山"]
    assert build_transcript_text([])[0] == ""
