"""tests/test_record_inject.py — 最上級クエリの決定論注入

★2026-08-16 海山「シンガポールの過去3年の最高売上は?」に誤答した件の回帰網。
故障の本体は「最大値の算出を LLM に任せていたこと」で、bot は wiki が持つ全日でなく
ベクトル検索が返した断片の中の最大を「最高」と答えていた (実際は同 wiki 内にもっと
高い日があった)。

§1.15(b) cross-check 3 体が初版を NO-GO にした指摘 (収集開始日のハードコード /
0 円を記録扱い / 否定形で向きが反転 / 客数を売上でランキング / 当日の部分値混入 /
「タイ」の部分一致 / 複数国の宣言順) をすべてケース化してある。
"""
from __future__ import annotations

import json
from datetime import date
from pathlib import Path

import pytest

from brain_wiki_helpers.record_inject import build_context, detect_record_query, nation_daily

_TODAY = date(2026, 8, 16)

_MONTHLY = """---
clone_visibility: public
---
# OWNDAYS 月次

## 全体月次推移

| 月 | 売上 (JPY) | 客数 | 日商平均 | 客単価 |
|----|-----------|------|---------|--------|
| 2023-08 | 2,111,222,333円 | 224,857 | 107,912,941円 | 14,877円 |
| 2025-12 | 4,000,000,000円 | 250,000 | 129,032,258円 | 16,000円 |
| 2026-08 | 2,848,445,903円 | 146,552 | 189,896,393円 | 19,436円 |

## 国別月次推移 (主要国)

### シンガポール

| 月 | 売上 (現地通貨) | 客数 |
|----|---------------|------|
| 2023-12 | 6,999,111 SGD | 35,238 |
| 2024-12 | 7,111,222 SGD | 39,211 |
| 2025-12 | 7,654,321 SGD | 38,629 |
| 2026-02 | 3,111,222 SGD | 23,959 |
| 2026-08 | 2,999,888 SGD | 17,934 |

### 台湾

| 月 | 売上 (現地通貨) | 客数 |
|----|---------------|------|
| 2025-12 | 1,000,000 TWD | 10,000 |
"""

# 5列 (旧) と 7列 (YoY 付き = 2026-08-16 の union 化以降) を混在させる。
_NATION_DAILY = """---
clone_visibility: public
---
# 国別 日次履歴

## 2026-05-25 (月)

| # | 名称 | 客数 | 売上 (JPY) | 客単価 (JPY) |
|---|------|------|-----------|-------------|
| 1 | 日本 | 5,000 | 64,000,000 | 16,000 |
| 2 | シンガポール | 1,500 | 36,000,000 | 30,000 |
| 3 | ベトナム | 0 | 0 | 0 |

## 2026-08-08 (土)

| # | 名称 | 客数 | 売上 (JPY) | 客単価 (JPY) | 前年同期 (現地通貨) | 前年比 % |
|---|------|------|-----------|-------------|-------------------|---------|
| 1 | 日本 | 5,626 | 70,000,111 | 12,441 | 75,000,222 JPY | 93.1% |
| 2 | シンガポール | 1,648 | 41,111,111 | 24,946 | 377,000 SGD | 108.7% |
| 3 | ベトナム | 0 | 0 | 0 | — | — |

## 2026-08-10 (月)

| # | 名称 | 客数 | 売上 (JPY) | 客単価 (JPY) | 前年同期 (現地通貨) | 前年比 % |
|---|------|------|-----------|-------------|-------------------|---------|
| 1 | 日本 | 9,946 | 64,000,000 | 8,043 | 78,000,000 JPY | 102.6% |
| 2 | シンガポール | 1,710 | 42,222,222 | 24,691 | 347,248 SGD | 132.4% |

## 2026-08-15 (土)

| # | 名称 | 客数 | 売上 (JPY) | 客単価 (JPY) | 前年同期 (現地通貨) | 前年比 % |
|---|------|------|-----------|-------------|-------------------|---------|
| 1 | 日本 | 6,020 | 77,777,111 | 12,919 | 75,000,222 JPY | 103.2% |
| 2 | シンガポール | 1,879 | 47,777,777 | 25,414 | 347,248 SGD | 132.4% |

## 2026-08-16 (日)

| # | 名称 | 客数 | 売上 (JPY) | 客単価 (JPY) |
|---|------|------|-----------|-------------|
| 1 | 日本 | 100 | 1,000,000 | 10,000 |
| 2 | シンガポール | 10 | 100,000 | 10,000 |

## 関連
- [[knowledge/owndays-history-monthly]]
"""

_TOTAL_DAILY = """---
clone_visibility: public
---
# OWNDAYS 日別全体売上 (過去履歴)

**期間**: 2023-08-10 ～ 2026-08-15 (3 日)

| 日付 | 曜日 | 売上 | 客数 |
|------|------|------|------|
| 2023-08-10 | (木) | 71,111,222円 | 6,093 |
| 2023-08-13 | (日) | 133,333,111円 | 10,905 |
| 2026-08-15 | (土) | 199,999,111円 | 12,111 |
"""


@pytest.fixture
def kd(tmp_path):
    (tmp_path / "owndays-history-monthly.md").write_text(_MONTHLY, encoding="utf-8")
    (tmp_path / "owndays-history-nationdaily.md").write_text(_NATION_DAILY, encoding="utf-8")
    (tmp_path / "owndays-history-totaldaily.md").write_text(_TOTAL_DAILY, encoding="utf-8")
    return tmp_path


def _b(q, kd, today=_TODAY):
    return build_context(q, today=today, knowledge_dir=kd)


# ─── 検出 ────────────────────────────────────────────────────────────────

@pytest.mark.parametrize("q", [
    "シンガポールの単日の過去最高売上は？",
    "シンガポールの過去3年での最高売上は？",
    "シンガポールで一番売れた日は？",
    "日本の売上の最高記録",
    "全社の過去最高の日商は？",
])
def test_detects_superlative_sales_queries(q):
    assert detect_record_query(q) is not None


@pytest.mark.parametrize("q,why", [
    ("シンガポールの昨日の売上は？", "最上級語が無い"),
    ("最高のチームを作るには", "売上文脈が無い"),
    ("売上の最高記録は？", "国も全社も明示されていない (日本 default にしない)"),
    ("日本橋店の最高売上", "日本橋は国名でない + 店舗粒度"),
    ("シンガポールで一番売れた商品は？", "商品粒度は担当外"),
    ("タイミングが最高の売上施策", "『タイ』の部分一致 (カナ続き)"),
    ("ネクタイの売上が最高だった月", "『タイ』の部分一致 (前がカナ)"),
    ("ピーク時の売上対応", "ピーク時は最上級でない"),
])
def test_does_not_fire_outside_scope(q, why):
    assert detect_record_query(q) is None, why


# ─── 向き (★critical: 否定形で反転していた) ──────────────────────────────

@pytest.mark.parametrize("q,want_min", [
    ("シンガポールの最高売上の日", False),
    ("シンガポールの最低売上の月", True),
    ("シンガポールで最も売れていない日は？", True),
    ("シンガポールで一番売れなかった日は？", True),
    ("日本で一番客数が少なかった日", True),
    ("日本で最も客数が多かった日", False),
    ("シンガポールで売上が最も高かった日", False),
])
def test_direction_including_negated_superlatives(q, want_min):
    det = detect_record_query(q)
    assert det is not None and det["want_min"] is want_min, q


# ─── メトリック (★high: 客数を聞かれて売上でランキングしていた) ───────────

def test_customer_count_ranks_by_customers_not_sales(kd):
    """日本: 売上最高=8/15 (97.6M) だが 客数最高=8/10 (9,946人)。"""
    block = _b("日本の過去最高の客数の日は？", kd)
    day = block[block.index("■ 日次"):]
    assert "1位 2026-08-10  9,946 人" in day, day
    assert "客数の最高" in day


def test_unit_price_ranks_by_unit_price(kd):
    block = _b("日本で客単価が最高だった日は？", kd)
    day = block[block.index("■ 日次"):]
    assert "1位 2026-08-15  12,919 円" in day, day


# ─── ランキングの正しさ ──────────────────────────────────────────────────

def test_daily_max_uses_all_days_not_a_fragment(kd):
    """事故そのもの: 8/8 でなく 8/15 が最高。"""
    day = _b("シンガポールの単日の過去最高売上は？", kd)
    day = day[day.index("■ 日次"):]
    assert "1位 2026-08-15  47,777,777 円" in day, day
    assert "2位 2026-08-10" in day


def test_monthly_max_is_the_largest_not_the_smallest(kd):
    head = _b("シンガポールの過去3年での最高売上は？", kd)
    head = head[head.index("■ 月次"):head.index("■ 日次")]
    assert "1位 2025-12  7,654,321 SGD" in head, head


def test_monthly_min_direction(kd):
    head = _b("シンガポールの月商が最低だった月は？", kd)
    head = head[head.index("■ 月次"):]
    assert "1位 2026-02  3,111,222 SGD" in head, head


# ─── 部分期間の除外 (★high: 当日が日次に混入していた) ────────────────────

def test_today_is_excluded_from_daily_ranking(kd):
    """8/16 は集計途中。最低クエリで部分日が『記録』になってはいけない。"""
    block = _b("シンガポールで一番売れなかった日は？", kd)
    day = block[block.index("■ 日次"):]
    ranks = [ln for ln in day.splitlines() if "位 " in ln]
    assert not any("2026-08-16" in ln for ln in ranks), ranks
    assert "1位 2026-05-25  36,000,000 円" in day, day
    assert "※本日 2026-08-16 は集計途中" in day


def test_in_progress_month_is_excluded_from_ranking(kd):
    assert "2026-08 は進行中" in _b("シンガポールの最高売上の月は？", kd)


# ─── 0 行 (★critical: 未出店国で「最高 0 円」を断定していた) ──────────────

def test_zero_only_country_does_not_claim_a_record(kd):
    block = _b("ベトナムの過去最高売上は？", kd)
    assert "1位" not in block, block
    assert "実績が無い" in block


# ─── 保持範囲は実データから (★critical: 誤った日付をハードコードしていた) ─

def test_daily_window_caveat_uses_actual_oldest_day(kd):
    block = _b("シンガポールの過去最高売上は？", kd)
    assert "2026-05-25 以降しか保持していない" in block, block
    # ハードコードした誤日付が復活していないこと
    assert "2026-04-24" not in block


def test_caveat_follows_the_data_when_backfilled(kd):
    """3 年 backfill 後は留保文が自動で追随する (ハードコードなら追随しない)。"""
    p = kd / "owndays-history-nationdaily.md"
    p.write_text(p.read_text(encoding="utf-8").replace("2026-05-25", "2023-08-10"),
                 encoding="utf-8")
    block = _b("シンガポールの過去最高売上は？", kd)
    assert "2023-08-10" in block, block          # 実データ由来 (ハードコードなら追随しない)
    assert "この範囲で断定してよい" in block      # 3年そろったので留保が緩む


# ─── 単位の混在 (★high) ──────────────────────────────────────────────────

def test_mixed_units_are_flagged(kd):
    """月次 SGD と日次 円 を並べる以上、比較禁止を明示する。"""
    block = _b("シンガポールの過去最高売上は？", kd)
    assert "SGD 建て" in block and "円 建て" in block
    assert "直接比較・合算してはいけない" in block


def test_japan_only_does_not_emit_a_bogus_unit_warning(kd):
    """日本は月次も日次も円 → 混在警告は出さない (ノイズにしない)。"""
    block = _b("日本の過去最高売上の日は？", kd)
    assert "単位が混在" not in block


# ─── 複数国 (★high: 宣言順で先頭 1 国だった) ─────────────────────────────

def test_multiple_countries_use_query_order(kd):
    block = _b("シンガポールと台湾で過去最高売上が高いのは？", kd)
    assert block.index("シンガポール の売上記録") < block.index("台湾 の売上記録"), block


# ─── 全社 (★medium: 未カバーだった。3 年分あるので本当に答えられる) ───────

def test_company_scope_uses_three_year_total_history(kd):
    block = _b("全社の過去最高の日商は？", kd)
    assert "全社の売上記録" in block
    day = block[block.index("■ 日次"):]
    assert "1位 2026-08-15  199,999,111 円" in day, day
    assert "2023-08-10" in day and "この範囲で断定してよい" in day, day


# ─── 期間指定 (★high: 期間語を無視して窓全体を出していた) ────────────────

def test_period_scoped_query_limits_the_ranking(kd):
    block = _b("シンガポールの先週で一番売れた日は？", kd, today=date(2026, 8, 12))
    day = block[block.index("■ 日次"):]
    assert "に限定" in day, day
    assert "2026-08-08" in day and "2026-05-25" not in day
    assert "■ 月次" not in block, "期間指定時に全期間の月次を並べない"


# ─── パーサの健全性 ──────────────────────────────────────────────────────

def test_seven_column_rows_take_sales_from_the_right_position(kd):
    """union 化で 7 列になった日でも、前年同期の値を売上と取り違えない。"""
    rows = dict((d, s) for d, s, _c in nation_daily("シンガポール", kd))
    assert rows["2026-08-08"] == 41_111_111
    assert 377_000 not in rows.values(), "前年同期 (現地通貨) を売上として拾っている"


def test_monthly_section_does_not_bleed_into_next_country(kd):
    from brain_wiki_helpers.record_inject import nation_monthly
    rows, currency = nation_monthly("シンガポール", kd)
    assert currency == "SGD"
    assert 1_000_000 not in [a for _ym, a, _c in rows], "台湾の行を吸い込んでいる"


def test_missing_files_return_none(tmp_path):
    assert build_context("シンガポールの過去最高売上は？", knowledge_dir=tmp_path) is None


# ─── 3年 backfill cache との統合 (★2026-08-16 Step 4) ────────────────────
# API は過去日も返すが、返る値は **確定値** で当日 snapshot より 0.3〜1.5% 高い
# (遅れて計上される取引。5 日で実測、方向は一貫して正)。混在させると backfill 期間
# だけが有利になり「記録」が偏るため、同じ日は確定値で上書きする。

def _write_backfill(kd, ymd, sgd, cust):
    d = kd.parent.parent / "import" / "owndays_history" / "nationdaily"
    d.mkdir(parents=True, exist_ok=True)
    (d / f"{ymd}.json").write_text(json.dumps([
        {"NationName": "シンガポール", "Amount": str(sgd), "DollarRate": "125.250000000",
         "DollarShort": "SGD", "CustomerCount": cust},
    ], ensure_ascii=False), encoding="utf-8")


@pytest.fixture
def kd_nested(tmp_path):
    """本番と同じ入れ子 (data/brain/wiki/knowledge と data/brain/import/…) を作る。"""
    kd = tmp_path / "wiki" / "knowledge"
    kd.mkdir(parents=True)
    (kd / "owndays-history-monthly.md").write_text(_MONTHLY, encoding="utf-8")
    (kd / "owndays-history-nationdaily.md").write_text(_NATION_DAILY, encoding="utf-8")
    return kd


def test_backfill_extends_history_before_the_snapshot_window(kd_nested):
    """snapshot が持たない過去日 (2023) を backfill が足す。"""
    _write_backfill(kd_nested, "2023-12-24", 799_999, 5_000)
    block = build_context("シンガポールの過去最高売上は？", today=_TODAY,
                          knowledge_dir=kd_nested)
    day = block[block.index("■ 日次"):]
    assert "1位 2023-12-24  100,199,874 円" in day, day
    assert "2023-12-24" in day, "保持範囲が backfill に追随していない"


def test_settled_value_overrides_same_day_snapshot(kd_nested):
    """同じ日が両方にあれば確定値を採る (系列の定義を揃える)。"""
    _write_backfill(kd_nested, "2026-08-08", 412_477, 1_659)   # 実測の確定値
    rows = dict((d, s) for d, s, _c in nation_daily("シンガポール", kd_nested))
    assert rows["2026-08-08"] == int(412_477 * 125.25), rows["2026-08-08"]
    assert rows["2026-08-08"] != 41_111_111, "当日 snapshot が残っている"


def test_no_backfill_dir_is_harmless(kd_nested):
    assert build_context("シンガポールの過去最高売上は？", today=_TODAY,
                         knowledge_dir=kd_nested) is not None


def test_caveat_relaxes_once_three_years_are_covered(kd_nested):
    """3年分そろったら「断定するな」を「この範囲で断定してよい」に変える。

    答えられる問いに答えない縮退 (backfill 前の文面の残留) を防ぐ。
    """
    _write_backfill(kd_nested, "2023-08-10", 100_000, 1_000)
    block = build_context("シンガポールの過去3年での最高売上は？", today=_TODAY,
                          knowledge_dir=kd_nested)
    assert "この範囲で断定してよい" in block, block
    assert "日次で断定してはいけない" not in block


def test_caveat_stays_strict_for_a_short_window(kd_nested):
    block = build_context("シンガポールの過去3年での最高売上は？", today=_TODAY,
                          knowledge_dir=kd_nested)
    assert "日次で断定してはいけない" in block, "短い窓なのに留保が緩んでいる"
