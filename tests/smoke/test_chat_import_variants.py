"""smoke: chat_import の 2026 export variant 対応 (★2026-07-05)

1. LINE 空白区切り variant: "HH:MM Sender Message" (sender に空白含む) + 複数行継続 +
   ドット日付 "2026.04.17 金曜日" — 従来 parser は日付不match・継続行を全損していた
2. WhatsApp iOS 日本語ロケール: "[24/7/25 午後5:46:23] Sender: msg" → 24h 変換
3. 従来 (タブ区切り / 英語 WhatsApp) の regression
LLM/network 非依存 (純粋 parser のみ)。
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from chat_import import (  # noqa: E402
    detect_chat_format,
    parse_line_export,
    parse_whatsapp_export,
)

LINE_SPACE_SAMPLE = """2026.04.17 金曜日
17:45 aida aidaがTake Umiyamaをグループに追加しました。
17:46 会田次郎 @All グループ作りました
18:36 Take Umiyama 宜しくお願いします。
18:59 会田次郎 ざっくりと備忘録です。
・KPIを2つに分ける
・全社と部門のKPIを整理
19:01 Take Umiyama ありがとうございます
2026.04.20 月曜日
20:57 aida 次回日程の候補です
21:02 Take Umiyama 5/1大丈夫です。
21:43 aida ありがとうございます。
23:03 Take Umiyama 11時とかなら大丈夫。
"""

WA_JP_SAMPLE = """[24/7/25 午後5:46:23] John Smith: Hi Take San ... quick update
[24/7/25 午後5:46:28] John Smith: we are sorted for 4 banks
[25/7/25 午前9:12:00] Take Umiyama: Thanks, will check
[25/7/25 午前12:05:00] Take Umiyama: midnight edge
[25/7/25 午後12:30:00] John Smith: noon edge
[26/7/25 午後11:59:59] Take Umiyama: multi
line body
"""

LINE_TAB_SAMPLE = "2026/04/14(月)\n10:00\tTaro\tおはよう\n10:01\tJiro\tおはようございます\n10:02\tTaro\tよろしく\n"


@pytest.mark.smoke
def test_line_space_variant_parses_multitoken_sender(tmp_path):
    f = tmp_path / "20260705_LINETGBoard.txt"
    f.write_text(LINE_SPACE_SAMPLE, encoding="utf-8")
    assert detect_chat_format(LINE_SPACE_SAMPLE) == "line"
    msgs = parse_line_export(f)
    senders = {m["sender"] for m in msgs}
    # 空白を含む sender が正しく境界検出される (頻度ベース最長一致)
    assert "Take Umiyama" in senders
    assert "aida" in senders and "会田次郎" in senders
    # "Take" 単独 や "Take Umiyama 宜しくお願いします。" のような誤分割が無い
    assert "Take" not in senders
    # 全メッセージに正規化済み日付が付く (ドット → ハイフン)
    assert all(m["date"] for m in msgs)
    assert msgs[0]["date"] == "2026-04-17"
    assert msgs[-1]["date"] == "2026-04-20"


@pytest.mark.smoke
def test_line_space_variant_keeps_multiline_body(tmp_path):
    """従来 parser が捨てていた継続行 (箇条書き等) を本文として保全。"""
    f = tmp_path / "20260705_LINETGBoard.txt"
    f.write_text(LINE_SPACE_SAMPLE, encoding="utf-8")
    msgs = parse_line_export(f)
    memo = next(m for m in msgs if "備忘録" in m["text"])
    assert "KPIを2つに分ける" in memo["text"]
    assert "全社と部門のKPIを整理" in memo["text"]
    # 継続行が独立メッセージとして誤カウントされていない
    assert not any(m["text"].startswith("・KPI") and m["sender"] != memo["sender"] for m in msgs)


@pytest.mark.smoke
def test_line_tab_format_regression(tmp_path):
    """従来のタブ区切り形式は不変に parse できる。"""
    f = tmp_path / "line_chat_LINEtaro.txt"
    f.write_text(LINE_TAB_SAMPLE, encoding="utf-8")
    msgs = parse_line_export(f)
    assert [m["sender"] for m in msgs] == ["Taro", "Jiro", "Taro"]
    assert msgs[0]["date"] == "2026-04-14"   # 正規化 (旧: 2026/04/14)


@pytest.mark.smoke
def test_whatsapp_jp_locale_am_pm(tmp_path):
    f = tmp_path / "WhatsApp_Chat_-_John_Smith.txt"
    f.write_text(WA_JP_SAMPLE, encoding="utf-8")
    assert detect_chat_format(WA_JP_SAMPLE) == "whatsapp"
    msgs = parse_whatsapp_export(f)
    assert len(msgs) == 6
    times = [m["time"] for m in msgs]
    assert times[0] == "17:46:23"       # 午後5時 → 17
    assert times[2] == "09:12:00"       # 午前9時 → 09
    assert times[3] == "00:05:00"       # 午前12時 = 深夜0時
    assert times[4] == "12:30:00"       # 午後12時 = 正午12時
    assert times[5] == "23:59:59"
    assert msgs[0]["date"] == "2025-07-24"   # DD/M/YY → ISO
    assert "multi\nline body" in msgs[5]["text"]
    assert {m["sender"] for m in msgs} == {"John Smith", "Take Umiyama"}
