"""tests/smoke/test_yoy_and_followup.py

YoY (year-over-year) 注入 + 売上フォローアップの単体テスト。

1. YoY (yoy_inject): 既存店=公式 MTD まとめ / 全店=完了月 monthly.json、日次自前 YoY は作らない。
2. フォローアップ (business_intent.is_business_followup + daily_history_inject 国名認識):
   「日本の」型が売上会話の継続として拾われ nation データが注入される。

注: 本ファイルの売上・客数の数値はすべてダミー (公開用サニタイズ済み)。検証しているのは
    比率計算・分岐ロジックであり、実売上ではない。
"""
from __future__ import annotations

import json
from datetime import date

import pytest


# ─── YoY ───

@pytest.fixture()
def _yoy_dirs(tmp_path):
    kd = tmp_path / "knowledge"
    kd.mkdir()
    (kd / "owndays-monday-dash-latest.md").write_text(
        "---\nupdated: 2026-07-19\n---\n# Monday Dash\n\n### ★ MTD まとめ (= 既存店前年比 月次 canonical)\n\n"
        "**月間累計 (19日まで、全店)**\n"
        "- **全店売上**: **1,200,000,000円** (店舗予算比 **95%**)\n"
        "- **★ 既存店前年比 (曜日対比)**: 売上 **121%** / 客数 **110%** / 客単価 **111%**\n"
        "- **★ 既存店前年比 (同日対比)**: 売上 **123%** / 客数 **112%** / 客単価 **110%**\n\n"
        "**直近単日 (7月19日、全店)**\n"
        "- 全店売上: 95,000,000円\n"
        "- 既存店前年比 曜日対比: 売上 134% / 客数 135% / 客単価 99%\n\n"
        "**注**: これは無視される行 おはようございます OWNDAYS NET 生ログ\n",
        encoding="utf-8")
    imp = tmp_path / "import"
    imp.mkdir()
    (imp / "monthly.json").write_text(json.dumps({
        "2026-06": {"start": "2026-06-01", "end": "2026-06-30",
                    "total": {"JPYAmount": "5800000000", "CustomerCount": 265000}},
        "2025-06": {"start": "2025-06-01", "end": "2025-06-30",
                    "total": {"JPYAmount": "5000000000", "CustomerCount": 250000}},
        # partial 月 (end が月末でない = 取込途中) — 完了扱いしてはいけない
        "2026-05": {"start": "2026-05-01", "end": "2026-05-18",
                    "total": {"JPYAmount": "1000000000", "CustomerCount": 60000}},
        "2025-05": {"start": "2025-05-01", "end": "2025-05-31",
                    "total": {"JPYAmount": "4000000000", "CustomerCount": 250000}},
    }), encoding="utf-8")
    return imp, kd


def test_yoy_intent_detection():
    from brain_wiki_helpers.yoy_inject import detect_yoy_intent
    for q in ("既存店前年比は?", "全店の昨年対比", "客単価の前年比", "6月の対前年", "昨対どう?"):
        assert detect_yoy_intent(q), q
    for q in ("昨日の売上は?", "今日の予定"):
        assert not detect_yoy_intent(q), q


def test_yoy_existing_store_from_monday_dash(_yoy_dirs):
    from brain_wiki_helpers.yoy_inject import build_yoy_context
    imp, kd = _yoy_dirs
    out = build_yoy_context("既存店の昨年対比", today=date(2026, 7, 20), import_dir=imp, knowledge_dir=kd)
    assert "既存店前年比 (曜日対比): 売上 121%" in out
    assert "客数 112%" in out and "客単価 110%" in out  # 同日対比
    # 生ログ行・古い個別例は拾わない
    assert "おはようございます" not in out and "OWNDAYS NET" not in out
    assert "日次の既存店前年比は社内に確定データが無い" in out


def test_yoy_allstore_completed_month(_yoy_dirs):
    from brain_wiki_helpers.yoy_inject import build_yoy_context
    imp, kd = _yoy_dirs
    out = build_yoy_context("6月の全店前年比", today=date(2026, 7, 20), import_dir=imp, knowledge_dir=kd)
    assert "全店 前年比 (月次・完了月 2026-06" in out
    assert "売上 116%" in out and "客数 106%" in out  # 5800000000/5000000000=116%, 265000/250000=106%


def test_yoy_current_month_no_allstore(_yoy_dirs):
    """当月(進行中)は完了月でないので全店月次YoYを出さない(MTD YoY 捏造防止)。"""
    from brain_wiki_helpers.yoy_inject import build_yoy_context
    imp, kd = _yoy_dirs
    out = build_yoy_context("7月の全店前年比", today=date(2026, 7, 20), import_dir=imp, knowledge_dir=kd)
    assert "全店 前年比 (月次・完了月" not in out  # 当月は出さない
    assert "既存店前年比" in out  # 既存店(公式)は出る


def test_yoy_partial_month_not_treated_as_complete(_yoy_dirs):
    """★cross-check DA: end が月末でない partial 月 (取込途中) を完了月扱いしない
    (過小 YoY を確定値として出す事故防止)。"""
    from brain_wiki_helpers.yoy_inject import build_yoy_context
    imp, kd = _yoy_dirs
    out = build_yoy_context("5月の全店前年比", today=date(2026, 7, 20), import_dir=imp, knowledge_dir=kd)
    assert "全店 前年比 (月次・完了月 2026-05" not in out  # partial なので出さない


def test_yoy_existing_store_shows_content_asof_and_stale_warning(_yoy_dirs):
    """★cross-check DA HIGH: 既存店ブロックの基準日は**中身の日付**(build 日でなく)。古ければ警告。"""
    from brain_wiki_helpers.yoy_inject import build_yoy_context
    imp, kd = _yoy_dirs
    # fixture の直近単日は「7月19日」。today 7/20 → 1日前・警告なし
    out = build_yoy_context("既存店前年比", today=date(2026, 7, 20), import_dir=imp, knowledge_dir=kd)
    assert "データ基準日: 2026-07-19" in out
    assert "古い可能性" not in out
    # today を 8/20 にすると中身(7/19)が 32日前 → stale 警告 (frontmatter build 日ではなく中身で判定)
    out2 = build_yoy_context("既存店前年比", today=date(2026, 8, 20), import_dir=imp, knowledge_dir=kd)
    assert "データ基準日: 2026-07-19" in out2 and "古い可能性" in out2


def test_yoy_no_data_is_honest(tmp_path):
    from brain_wiki_helpers.yoy_inject import build_yoy_context
    out = build_yoy_context("昨年対比", today=date(2026, 7, 20),
                            import_dir=tmp_path / "x", knowledge_dir=tmp_path / "y")
    assert "確定データが手元に無い" in out and "推測で" in out


def test_yoy_non_intent_returns_none(_yoy_dirs):
    from brain_wiki_helpers.yoy_inject import build_yoy_context
    imp, kd = _yoy_dirs
    assert build_yoy_context("今日の天気", today=date(2026, 7, 20), import_dir=imp, knowledge_dir=kd) is None


# ─── 日次の既存店売上前年比 (店舗別 API yAmount) ───

@pytest.fixture()
def _storelist_dir(tmp_path):
    """単日・店舗別 JSON (当日 Amount + 前年 yAmount + DollarRate)。数値はダミー。"""
    d = tmp_path / "import"
    d.mkdir()
    rows = [
        # 日本 (JPY, DollarRate 1) — 既存店 (yAmount>0)
        {"StoreNo": 1, "DollarShort": "JPY", "Amount": "1000000", "yAmount": 900000,
         "DollarRate": "1.0", "CustomerCount": 60},
        {"StoreNo": 2, "DollarShort": "JPY", "Amount": "500000", "yAmount": 600000,
         "DollarRate": "1.0", "CustomerCount": 30},
        # 日本 新店 (yAmount=0 → 既存店から除外)
        {"StoreNo": 3, "DollarShort": "JPY", "Amount": "300000", "yAmount": 0,
         "DollarRate": "1.0", "CustomerCount": 20},
        # 台湾 (TWD, DollarRate 5) — 既存店
        {"StoreNo": 101, "DollarShort": "TWD", "Amount": "100000", "yAmount": 80000,
         "DollarRate": "5.0", "CustomerCount": 40},
    ]
    (d / "owndays_mobile_api_storelist_2026-07-19.json").write_text(
        json.dumps(rows), encoding="utf-8")
    # 当日 (2026-07-20) = 集計途中の partial ファイル (Amount が小さい)
    partial = [{"StoreNo": 1, "DollarShort": "JPY", "Amount": "100000", "yAmount": 900000,
                "DollarRate": "1.0", "CustomerCount": 5}]
    (d / "owndays_mobile_api_storelist_2026-07-20.json").write_text(
        json.dumps(partial), encoding="utf-8")
    return d


def test_daily_existing_store_japan_exact(_storelist_dir):
    """★日本(単一通貨)= 為替歪みなしで正確。新店(yAmount=0)は既存店から除外。
    客単価は全店の実額 (既存店でなく)。"""
    from brain_wiki_helpers.yoy_inject import _daily_existing_store_block
    out = _daily_existing_store_block("昨日の日本の既存店前年比", date(2026, 7, 20), _storelist_dir)
    # 既存店 = 店1(100万/90万)+店2(50万/60万) = 150万/150万 = 100.0%、店3(新店)除外
    assert "売上前年比: 100.0%" in out and "既存店 2店" in out
    assert "日本" in out
    # 客単価は全店の実額: (100万+50万+30万[新店含む])/(60+30+20客) = 180万/110 = 16,364円
    assert "全店 客単価 (当日実額" in out and "16,364円" in out
    assert "既存店客単価前年比" in out  # 既存店客単価は不可の注記


def test_daily_existing_store_allcompany_by_nation(_storelist_dir):
    """★海山指示: 全社は為替概算1値でなく国別内訳を並べる。"""
    from brain_wiki_helpers.yoy_inject import _daily_existing_store_block
    out = _daily_existing_store_block("昨日の全社の既存店前年比", date(2026, 7, 20), _storelist_dir)
    assert "国別" in out and "合算せず国別で表示" in out
    assert "日本: 売上 100.0% (2店)" in out  # 日本 150万/150万
    assert "台湾: 売上 125.0% (1店)" in out   # 台湾 10万/8万 = 125%
    assert "全社=" not in out  # 為替換算の全社1値は出さない


def test_daily_existing_store_no_date_defers_to_official(_storelist_dir):
    """日付が無い『既存店前年比』は日次ブロックを出さず本部公式へ委ねる (None)。"""
    from brain_wiki_helpers.yoy_inject import _daily_existing_store_block
    assert _daily_existing_store_block("既存店前年比は?", date(2026, 7, 20), _storelist_dir) is None


def test_daily_existing_store_missing_file_honest(_storelist_dir):
    from brain_wiki_helpers.yoy_inject import _daily_existing_store_block
    out = _daily_existing_store_block("一昨日の日本の既存店前年比", date(2026, 7, 20), _storelist_dir)
    assert out is not None and "手元に無い" in out  # 2026-07-18 の JSON は無い


def test_daily_existing_store_today_excluded(_storelist_dir):
    """★cross-check critical: 当日(集計途中)は前年比を出さない (partial÷終日=壊れた%を防ぐ)。"""
    from brain_wiki_helpers.yoy_inject import _daily_existing_store_block
    # today=2026-07-20 で「今日/本日」→ 当日のみ → % を出さず「集計途中」
    out = _daily_existing_store_block("今日の日本の既存店前年比", date(2026, 7, 20), _storelist_dir)
    assert out is not None and "集計途中" in out
    assert "%" not in out.replace("既存店", "")  # 壊れた比率を出さない


def test_daily_existing_store_range_excludes_running_day(_storelist_dir):
    """今週(進行中当日を含む)は当日を除外し、部分合計を明示。"""
    from brain_wiki_helpers.yoy_inject import _daily_existing_store_block
    # today=2026-07-20(月)、今週=07-20のみ→当日除外で complete_dates 空→集計途中
    out = _daily_existing_store_block("今週の日本の既存店前年比", date(2026, 7, 20), _storelist_dir)
    assert "集計途中" in out


def test_daily_existing_store_area_declines(_storelist_dir):
    """エリア/業態は日次店舗別では正確に切れない → 算出不可を明示 (全社に化けない)。"""
    from brain_wiki_helpers.yoy_inject import _daily_existing_store_block
    out = _daily_existing_store_block("昨日の関東エリアの既存店前年比", date(2026, 7, 20), _storelist_dir)
    assert out is not None and "算出不可" in out and "国単位" in out


# ─── フォローアップ (日本の) ───

def test_japan_nation_recognized_safely():
    from brain_wiki_helpers.daily_history_inject import detect_dimension
    assert detect_dimension("昨日の売上は? 日本の") == ("nation", ["日本"])
    assert detect_dimension("日本の売上") == ("nation", ["日本"])
    # 非国名の複合語は「日本」を tokens に入れない (default 日本 に落ちるが 日本一 では nation=日本 default)
    for q in ("日本語の売上資料",):
        d = detect_dimension(q)
        # scope 明示無し扱い → default 日本 (token=[日本]) だが 日本語 は誤 nation でない
        assert d == ("nation", ["日本"])  # default 日本 (誤爆でなく scope 無しの default)


def test_sales_scope_default_japan():
    """★海山: scope 明示無しの売上は日本 default。全社/他国は明示時のみ。"""
    from brain_wiki_helpers.daily_history_inject import detect_dimension
    assert detect_dimension("昨日の売上") == ("nation", ["日本"])       # default 日本
    assert detect_dimension("先週の客数は?") == ("nation", ["日本"])    # default 日本
    assert detect_dimension("昨日の全社の売上") is None                 # 全社 → totaldaily 経路
    assert detect_dimension("グローバルの売上") is None                 # グローバル明示
    assert detect_dimension("海外の売上") is None                       # 海外明示
    assert detect_dimension("台湾の売上") == ("nation", ["台湾"])        # 他国明示
    assert detect_dimension("昨日のエリア別売上")[0] == "area"           # エリア明示
    assert detect_dimension("今日の天気") is None                       # 非売上


def _write_trend_fixture(d):
    """実 areatotal JSON は AreaName にエリア接尾 (中部エリア 等)、rollup 合算行 (関東エリア=当年0) も
    併存。数値はダミー (公開用サニタイズ済み)。"""
    import json as _j
    (d / "owndays_mobile_api_nationtotal_2026-07-19.json").write_text(_j.dumps([
        {"NationName": "日本", "Amount": "99000000", "yAmount": 100000000},
        {"NationName": "台湾", "Amount": "30000000", "yAmount": 28000000},
    ]), encoding="utf-8")
    (d / "owndays_mobile_api_areatotal_2026-07-19.json").write_text(_j.dumps([
        {"AreaName": "中部エリア", "Amount": "10200000", "yAmount": 10000000},
        {"AreaName": "沖縄エリア", "Amount": "8100000", "yAmount": 10000000},
        {"AreaName": "関東エリア", "Amount": "0", "yAmount": 40000000},   # ★rollup 合算 (当年0) = 除外
        {"AreaName": "TH West", "Amount": "50000", "yAmount": 100000},    # 海外 = 除外
    ]), encoding="utf-8")


def test_japan_trend_block(tmp_path):
    """★日本の趨勢: 日本全店前年比 + エリア別前年比。rollup(当年0)・海外は除外。当日除外。"""
    from brain_wiki_helpers.yoy_inject import build_japan_trend
    d = tmp_path / "import"; d.mkdir()
    _write_trend_fixture(d)
    out = build_japan_trend("昨日の売上", today=date(2026, 7, 20), import_dir=d)
    assert "日本 全店 売上前年比: 99.0%" in out
    assert "中部 102%" in out and "沖縄 81%" in out  # 高い順 (中部先)
    assert out.index("中部") < out.index("沖縄")
    # ★rollup「関東 0%」・海外は漏れない
    assert "関東 0%" not in out and "TH West" not in out and "West" not in out
    # ★母集団 (全店ベース) と既存店との区別を明示
    assert "全店ベース" in out and "既存店" in out
    # 前年比%のみ (raw 前年円は出さない = guard 誤検知回避)
    assert "100,000,000" not in out


def test_japan_trend_not_for_allco_or_today_or_othernation(tmp_path):
    from brain_wiki_helpers.yoy_inject import build_japan_trend
    d = tmp_path / "import"; d.mkdir()
    _write_trend_fixture(d)
    assert build_japan_trend("昨日の全社の売上", today=date(2026, 7, 20), import_dir=d) is None  # 全社
    assert build_japan_trend("今日の売上", today=date(2026, 7, 20), import_dir=d) is None        # 当日のみ
    assert build_japan_trend("今日の天気", today=date(2026, 7, 20), import_dir=d) is None        # 非売上
    # ★cross-check: 台湾のエリア別 → 日本趨勢を誤貼りしない
    assert build_japan_trend("昨日の台湾のエリア別売上", today=date(2026, 7, 20), import_dir=d) is None


def test_zensten_not_treated_as_allco():
    """★cross-check: 全店 は日本 scope の all-store 概念 = 全社扱いしない (捏造経路に落とさない)。"""
    from brain_wiki_helpers.daily_history_inject import detect_dimension
    assert detect_dimension("昨日の全店の売上") == ("nation", ["日本"])   # 全店 → 日本 default
    assert detect_dimension("関東エリア全体の売上")[0] == "area"          # 全体 → area 保持
    assert detect_dimension("昨日の全部の売上") is None                   # 全部 → 全社


def test_today_only_defers_to_live(tmp_path):
    """★cross-check: 当日のみは build_context が None (live daily-sales に委ねる)。"""
    from brain_wiki_helpers.daily_history_inject import build_context
    kd = tmp_path / "knowledge"; kd.mkdir()
    (kd / "owndays-history-nationdaily.md").write_text("# x\n", encoding="utf-8")
    assert build_context("今日の売上", today=date(2026, 7, 20), knowledge_dir=kd) is None
    assert build_context("本日の日本の売上", today=date(2026, 7, 20), knowledge_dir=kd) is None


def test_is_business_followup():
    from brain_wiki_helpers.business_intent import is_business_followup
    for q in ("日本の", "台湾は?", "エリア別で", "既存店は?", "関東の", "国別で", "前年比も"):
        assert is_business_followup(q), q
    for q in ("ありがとう", "了解", "今日は疲れた", "その資料を明日までにまとめて長い文章がここに続く"):
        assert not is_business_followup(q), q
    # ★cross-check DA: 国名/エリア語を含む非売上の雑談は除外
    for q in ("台湾行きたい", "日本の天気は?", "沖縄旅行したい", "台湾料理食べたい"):
        assert not is_business_followup(q), f"非売上雑談を誤爆: {q!r}"


def test_extract_date_phrase_carries_only_date():
    """★cross-check DA 次元シャドウ防止: 前クエリから日付だけ抽出 (次元語は捨てる)。"""
    from brain_wiki_helpers.business_intent import extract_date_phrase
    assert extract_date_phrase("先週の関東の売上は?") == "先週"
    assert extract_date_phrase("昨日の業態別売上") == "昨日"
    assert extract_date_phrase("7月15日の売上") == "7月15日"
    assert extract_date_phrase("売上教えて") == ""


def test_followup_dimension_not_shadowed():
    """業態別の後に「国別で」→ 業態でなく国別が効く (日付だけ引き継ぐ)。"""
    from brain_wiki_helpers.business_intent import extract_date_phrase
    from brain_wiki_helpers.daily_history_inject import detect_dimension
    dp = extract_date_phrase("昨日の業態別売上")  # → 昨日
    effective = f"{dp} 日本の売上"  # 今回の次元(国別)を優先
    assert detect_dimension(effective) == ("nation", ["日本"])  # 業態でなく国別
