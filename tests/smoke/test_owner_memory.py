"""tests/smoke/test_owner_memory.py — 恒久 owner-memory (★2026-07-20 個人エージェント評価 #1)。

services/owner_memory.py の pure function 群: メモリ add/dedup/del/注入 bounds、
タスク add/complete、リマインダー書込 (clone_reminder_check 互換 format)、
/memory コマンド、抽出 JSON parse。全て tmp_path 隔離 (BRAIN_APP_ROOT env)。
"""
from __future__ import annotations

import asyncio
import json

import pytest

from services import owner_memory as om


@pytest.fixture(autouse=True)
def _isolate(tmp_path, monkeypatch):
    monkeypatch.setenv("BRAIN_APP_ROOT", str(tmp_path))
    monkeypatch.setenv("OWNER_MEMORY_NOTIFY", "0")  # 通知は専用 test でのみ検証
    yield


# ─── メモリ ───

def test_add_and_parse_roundtrip():
    assert om.add_entry("facts", "出張の定宿はサンプルホテル")
    assert om.add_entry("preferences", "資料は結論先出しが好み")
    entries = om.parse_entries()
    assert len(entries) == 2
    assert entries[0]["section"] == "facts"
    assert "サンプルホテル" in entries[0]["text"]


def test_add_dedup_normalized():
    assert om.add_entry("facts", "定宿はサンプルホテル。")
    # 句読点/空白ゆらぎは同一視
    assert not om.add_entry("facts", "定宿はサンプルホテル")
    assert len(om.parse_entries()) == 1


def test_normalize_keeps_ascii_dot_decimal_not_collapsed():
    """★cross-check Reviewer: '.' 除去だと 10.5% と 105% が同一視され別事実が無音破棄される。"""
    assert om.add_entry("facts", "利益率10.5%が目標")
    assert om.add_entry("facts", "利益率105%が目標")  # 別事実として保存される
    assert len(om.parse_entries()) == 2


def test_auto_tag_roundtrip_and_display():
    """★cross-check DA: 自動抽出は (auto) tag で手動と区別 (汚染発覚時の判別)。"""
    assert om.add_entry("facts", "自動で覚えた事実", auto=True)
    assert om.add_entry("facts", "手動の事実")
    entries = om.parse_entries()
    assert entries[0]["auto"] is True and entries[1]["auto"] is False
    disp = om.format_display()
    assert "⚙" in disp and "自動で覚えた事実" in disp


def test_add_rejects_bad_section_and_empty():
    assert not om.add_entry("bogus", "テキスト")
    assert not om.add_entry("facts", "  ")
    assert om.parse_entries() == []


def test_remove_entry_by_display_index():
    om.add_entry("ongoing", "A案件進行中")
    om.add_entry("facts", "B事実")
    # 表示順 = facts が先 → 1 = B事実
    removed = om.remove_entry(1)
    assert removed == "B事実"
    remaining = om.parse_entries()
    assert len(remaining) == 1 and remaining[0]["text"] == "A案件進行中"
    assert om.remove_entry(99) is None


def test_load_memory_block_bounds_and_ongoing_cutoff():
    om.add_entry("facts", "恒久事実X")
    om.add_entry("ongoing", "昔の案件", date="2020-01-01")  # 90日 cutoff で注入外
    om.add_entry("ongoing", "今の案件")
    block = om.load_memory_block()
    assert "恒久事実X" in block and "今の案件" in block
    assert "昔の案件" not in block
    # bounds
    for i in range(80):
        om.add_entry("facts", f"事実その{i}ですかなり長い文章をここに書いて膨らませる")
    assert len(om.load_memory_block(max_chars=1600)) <= 1600 + 50


def test_load_memory_block_empty_is_empty_string():
    assert om.load_memory_block() == ""


# ─── タスク ───

def test_task_add_complete_open():
    assert om.add_task("Aさんに電話")
    assert not om.add_task("Aさんに電話")  # dedup
    assert om.add_task("資料レビュー")
    assert len(om.open_tasks()) == 2
    done = om.complete_task("電話")
    assert done and "電話" in done
    assert len(om.open_tasks()) == 1
    assert om.complete_task("存在しない") is None
    # 完了済は再完了できない
    assert om.complete_task("電話") is None
    # ★cross-check Reviewer: 正規化後空の match ('!' 等) で先頭タスクを誤完了しない
    assert om.complete_task("!") is None
    assert len(om.open_tasks()) == 1


# ─── リマインダー (clone_reminder_check 互換、auto/ 非追跡 subdir) ───

def test_create_reminder_writes_compatible_file():
    msg = om.create_reminder("2099-01-15", "本部会議の資料確認", "アジェンダを見ておく")
    assert "2099-01-15" in msg and "9:00" in msg
    # ★cross-check DA: bot 自動生成は git 非追跡の auto/ に書く
    p = om._reminders_dir() / "auto" / "2099-01-15.md"
    content = p.read_text(encoding="utf-8")
    # 1 行目が title (clone_reminder_check が Push title に使う)
    assert content.splitlines()[0] == "# 本部会議の資料確認"
    assert "アジェンダ" in content


def test_create_reminder_appends_same_day_and_dedups_title():
    om.create_reminder("2099-01-15", "その1")
    om.create_reminder("2099-01-15", "その2")
    content = (om._reminders_dir() / "auto" / "2099-01-15.md").read_text(encoding="utf-8")
    assert "その1" in content and "その2" in content
    # 同日同 title は重複設定しない
    msg = om.create_reminder("2099-01-15", "その1")
    assert "設定済み" in msg
    assert content == (om._reminders_dir() / "auto" / "2099-01-15.md").read_text(encoding="utf-8")


def test_create_reminder_validation():
    assert "エラー" in om.create_reminder("2020-01-01", "過去")
    assert "エラー" in om.create_reminder("15/01/2099", "形式違い")
    assert "エラー" in om.create_reminder("2099-01-15", "")
    # ★cross-check Reviewer: 暦上不正な日付 (regex は通る) を strptime で拒否
    assert "実在しない" in om.create_reminder("2099-13-45", "不正日付")


def test_create_reminder_rejects_today_after_delivery_hour(monkeypatch):
    """★cross-check 3体一致: 当日 09:00 以降の当日指定は永久未配送 → 拒否。"""
    from datetime import datetime
    real_now = om._now()
    monkeypatch.setattr(om, "_now", lambda: real_now.replace(hour=10, minute=0))
    today = om._today()
    assert "配信時刻を過ぎています" in om.create_reminder(today, "午後に思い出す")
    # 09:00 前なら当日 OK
    monkeypatch.setattr(om, "_now", lambda: real_now.replace(hour=8, minute=0))
    assert "届きます" in om.create_reminder(om._today(), "朝イチ確認")


def test_pending_reminders_listed_in_display():
    om.create_reminder("2099-01-15", "配信待ちの件")
    pend = om.pending_reminders()
    assert any("2099-01-15" in p and "配信待ちの件" in p for p in pend)
    assert "配信待ちの件" in om.format_display()


# ─── /memory コマンド ───

def test_memory_command_flow():
    assert "空です" in om.handle_memory_command("/memory")
    resp = om.handle_memory_command("/memory add 定宿はサンプルホテル")
    assert "記憶しました" in resp
    listing = om.handle_memory_command("/memory")
    assert "サンプルホテル" in listing and "1." in listing
    resp = om.handle_memory_command("/memory del 1")
    assert "削除" in resp
    assert om.parse_entries() == []
    # 非該当はフォールスルー (None)
    assert om.handle_memory_command("/memo これは別コマンド") is None
    assert "使い方" in om.handle_memory_command("/memory help")


# ─── 抽出 ───

class _FakeResp:
    def __init__(self, content):
        self._content = content
        self.status_code = 200

    def raise_for_status(self):
        pass

    def json(self):
        return {"choices": [{"message": {"content": self._content}}]}


class _FakeHttp:
    def __init__(self, content):
        self._content = content
        self.calls = []

    async def post(self, url, **kw):
        self.calls.append(kw)
        return _FakeResp(self._content)


def test_extract_from_turn_saves_items_as_auto():
    http = _FakeHttp(json.dumps({"items": [
        {"section": "facts", "text": "定宿はサンプルホテル"},
        {"section": "ongoing", "text": "X社と提携協議中"},
        {"section": "bogus", "text": "捨てられる"},
    ]}))
    n = asyncio.run(om.extract_from_turn(http, "来月の出張はいつものサンプルホテルで", "承知しました", "http://x", "k"))
    assert n == 2
    entries = om.parse_entries()
    texts = [e["text"] for e in entries]
    assert "定宿はサンプルホテル" in texts and "X社と提携協議中" in texts
    assert all(e["auto"] for e in entries)  # 自動抽出は auto tag 付き


def test_extract_notifies_umiyama_when_saved(monkeypatch):
    """★cross-check DA: auto 保存は LINE 1 行通知で「気付ける」を担保。"""
    import sys
    import types
    monkeypatch.setenv("OWNER_MEMORY_NOTIFY", "1")
    calls = []
    fake = types.ModuleType("clone_improve_lib")
    fake.line_push = lambda text: calls.append(text) or True
    # ★2026-07-20 通知削減: 保存通知は digest 経由 (1日2回まとめ)
    fake.line_push_digest = lambda text, component="": calls.append(text) or True
    monkeypatch.setitem(sys.modules, "clone_improve_lib", fake)
    http = _FakeHttp(json.dumps({"items": [{"section": "facts", "text": "通知テスト事実"}]}))
    n = asyncio.run(om.extract_from_turn(http, "u", "r", "http://x", "k"))
    assert n == 1
    assert calls and "通知テスト事実" in calls[0] and "/memory del" in calls[0]


def test_extract_from_turn_no_items():
    http = _FakeHttp('{"items": []}')
    assert asyncio.run(om.extract_from_turn(http, "おはよう", "おはようございます", "http://x", "k")) == 0
    assert om.parse_entries() == []


def test_post_turn_swallows_errors(monkeypatch):
    class _Boom:
        async def post(self, *a, **k):
            raise RuntimeError("litellm down")
    # 例外を上に漏らさない (fire-and-forget が本体を壊さない)
    asyncio.run(om.post_turn(_Boom(), "u", "r", "http://x", "k"))


def test_post_turn_respects_gate(monkeypatch):
    monkeypatch.setenv("OWNER_MEMORY_ENABLED", "0")
    called = []

    class _Http:
        async def post(self, *a, **k):
            called.append(1)
            return _FakeResp('{"items": []}')
    asyncio.run(om.post_turn(_Http(), "u", "r", "http://x", "k"))
    assert not called
