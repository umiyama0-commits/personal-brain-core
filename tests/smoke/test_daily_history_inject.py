"""tests/smoke/test_daily_history_inject.py — 日次売上の決定論注入の契約 pin (★2026-07-13).

failure-log 2026-07-13 (先週×関東の捏造事故) の再発防止装置。守る不変条件:
- 「先週」= 直前の月〜日 7 日間 (bot が月曜を落とした事故の再発防止)
- 集計は決定論 (「関東エリア」恒常 0 行は集計除外、A+B 合算が正しく出る)
- データが無い日は「無い」と明示 (推測で埋めさせない)
- sales_numeric_guard は注入に無い数値を検知して確定値を追記
- brain_wiki.py の配線 (build_context + guard) が存在する (source pin)
"""
from __future__ import annotations

from datetime import date
from pathlib import Path

from brain_wiki_helpers import daily_history_inject as dhi

_ROOT = Path(__file__).resolve().parents[2]

_FIXTURE = """---
updated: 2026-07-13
---
# OWNDAYS エリア別 日次履歴

## 2026-07-06 (月)

| # | 名称 | 客数 | 売上 (JPY) | 客単価 (JPY) |
|---|------|------|-----------|-------------|
| 1 | 関東Bエリア | 709 | 10,193,066 | 14,376 |
| 2 | 関東Aエリア | 689 | 9,654,969 | 14,013 |
| 3 | 九州Aエリア | 600 | 8,000,000 | 13,333 |
| 12 | 関東エリア | 0 | 0 | 0 |

## 2026-07-07 (火)

| # | 名称 | 客数 | 売上 (JPY) | 客単価 (JPY) |
|---|------|------|-----------|-------------|
| 1 | 関東Bエリア | 756 | 10,693,275 | 14,144 |
| 2 | 関東Aエリア | 653 | 9,332,988 | 14,292 |
| 12 | 関東エリア | 0 | 0 | 0 |
"""

_TODAY = date(2026, 7, 13)  # 月曜


def _knowledge_dir(tmp_path: Path) -> Path:
    (tmp_path / "owndays-history-areadaily.md").write_text(_FIXTURE, encoding="utf-8")
    return tmp_path


# ── 日付範囲 ─────────────────────────────────────────────
def test_last_week_is_mon_to_sun():
    dates, label = dhi.resolve_range("先週の関東エリアの売り上げ", _TODAY)
    assert dates[0] == date(2026, 7, 6) and dates[-1] == date(2026, 7, 12)
    assert len(dates) == 7 and "先週" in label


def test_last_last_week_and_yesterday():
    dates, _ = dhi.resolve_range("先々週の実績", _TODAY)
    assert dates[0] == date(2026, 6, 29) and dates[-1] == date(2026, 7, 5)
    dates, _ = dhi.resolve_range("昨日の売上", _TODAY)
    assert dates == [date(2026, 7, 12)]


def test_explicit_date_range_and_future_rolls_back():
    dates, _ = dhi.resolve_range("7月6日から7月8日の売上", _TODAY)
    assert dates == [date(2026, 7, 6), date(2026, 7, 7), date(2026, 7, 8)]
    dates, _ = dhi.resolve_range("12月25日の売上", _TODAY)  # 未来 → 前年
    assert dates == [date(2025, 12, 25)]


# ── 次元検出 ─────────────────────────────────────────────
def test_dimension_detection():
    assert dhi.detect_dimension("先週の関東エリアの売り上げ") == ("area", ["関東"])
    assert dhi.detect_dimension("先週の業態別売上") == ("type", [])
    assert dhi.detect_dimension("台湾の先週の売上") == ("nation", ["台湾"])
    assert dhi.detect_dimension("先週の天気") is None          # 売上系ワード無し
    # ★2026-07-20 海山: scope 明示無しの売上は default 日本 (全社/他国は明示時のみ)
    assert dhi.detect_dimension("先週の売上") == ("nation", ["日本"])
    assert dhi.detect_dimension("昨日の全社の売上") is None      # 全社明示 → 全社経路
    # 「日本一…」は 日本 を誤 token 化しない (default 日本 に落ちるが、日付が無いので注入されない)
    assert dhi.detect_dimension("日本一の売上を目指す") == ("nation", ["日本"])
    assert dhi.build_context("日本一の売上を目指す", knowledge_dir=None) is None  # 日付無し=注入なし


# ── 注入ブロック ─────────────────────────────────────────
def test_build_context_totals_and_missing(tmp_path):
    ctx = dhi.build_context("先週の関東エリアの売り上げについて教えて",
                            today=_TODAY, knowledge_dir=_knowledge_dir(tmp_path))
    assert ctx is not None
    # 決定論集計: A+B 合算 (7/06+7/07 の 2 日分)、恒常 0 行は除外
    assert "関東Bエリア: 客数 1,465 / 売上 20,886,341円" in ctx
    assert "関東Aエリア: 客数 1,342 / 売上 18,987,957円" in ctx
    assert "合計" in ctx and "客数 2,807" in ctx and "39,874,298円" in ctx
    # fixture に無い 7/08-7/12 は「データが無い」明示
    assert "2026-07-08" in ctx and "データが存在しない日" in ctx
    # 他エリアの行はフィルタで落ちる
    assert "九州" not in ctx


def test_build_context_partial_window_labeled(tmp_path):
    """★cross-check DA (D5b): 部分窓 (欠落日あり) の◆集計を「期間の合計」とラベルすると
    無音の過少報告に化ける → 部分合計の明示ラベルを pin。"""
    ctx = dhi.build_context("先週の関東エリアの売り上げ",
                            today=_TODAY, knowledge_dir=_knowledge_dir(tmp_path))
    assert "部分合計" in ctx and "期間全体の合計ではない" in ctx
    assert "期間全体の合計は算出できない" in ctx


def test_build_context_all_missing_returns_explicit_block(tmp_path):
    """★cross-check DA (D5a): 全欠落で None を返すと vector 経路に落ち捏造リスクが復活
    → 「データが無い」明示ブロックを返す (None ではない) ことを pin。"""
    kd = _knowledge_dir(tmp_path)
    ctx = dhi.build_context("6月1日から6月3日の関東エリアの売上",  # fixture に無い期間
                            today=_TODAY, knowledge_dir=kd)
    assert ctx is not None
    assert "存在しない" in ctx and "推測で数字を作らず" in ctx


def test_guard_allows_rounded_oku_man_expressions(tmp_path):
    """★cross-check DA (D2): 履歴表はプレーン円のみ = 億/万 token を含まない。丸め表現
    「約2,000万」等で毎回発火すると全売上応答が脚注だらけになる → 億/万 は検知対象外を pin。"""
    ctx = dhi.build_context("先週の関東エリアの売り上げ",
                            today=_TODAY, knowledge_dir=_knowledge_dir(tmp_path))
    rounded = "先週の関東は合計で約0.4億円、B が 2,000万円ちょい上回る感じだね。"
    assert dhi.sales_numeric_guard(rounded, ctx) == rounded  # 発火しない


def test_build_context_none_when_no_trigger(tmp_path):
    kd = _knowledge_dir(tmp_path)
    assert dhi.build_context("こんにちは", today=_TODAY, knowledge_dir=kd) is None
    assert dhi.build_context("先週は忙しかった", today=_TODAY, knowledge_dir=kd) is None
    assert dhi.build_context("関東エリアの明日の天気", today=_TODAY, knowledge_dir=kd) is None


# ── 数値ガード ───────────────────────────────────────────
def test_sales_numeric_guard_catches_fabrication(tmp_path):
    ctx = dhi.build_context("先週の関東エリアの売り上げ",
                            today=_TODAY, knowledge_dir=_knowledge_dir(tmp_path))
    fabricated = "先週の関東は客数 9,780、売上 111,222,333円だったよ。"
    out = dhi.sales_numeric_guard(fabricated, ctx)
    assert "確定値" in out and "20,886,341" in out  # 正値が決定論追記される


def test_sales_numeric_guard_passes_clean_reply(tmp_path):
    ctx = dhi.build_context("先週の関東エリアの売り上げ",
                            today=_TODAY, knowledge_dir=_knowledge_dir(tmp_path))
    clean = "関東Bは客数 1,465 / 売上 20,886,341円、Aは 1,342 / 18,987,957円だね。"
    assert dhi.sales_numeric_guard(clean, ctx) == clean


# ── brain_wiki 配線の source pin ─────────────────────────
def test_brain_wiki_wiring():
    src = (_ROOT / "brain_wiki.py").read_text(encoding="utf-8")
    assert "daily_history_inject import build_context" in src
    assert "sales_numeric_guard" in src
    # guard は sales_history_context がある時に _fac_guard 内で呼ばれる
    i_guard_def = src.index("def _fac_guard")
    assert "sales_numeric_guard" in src[i_guard_def:i_guard_def + 1200]
