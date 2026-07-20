"""brain_wiki_helpers/yoy_inject.py — 昨年対比 (YoY) の決定論注入
(★2026-07-20 海山「昨年対比も見れるようにしたい。全店/既存店/客数/客単価」)。

daily_history_inject と同じ設計 (pure function・LLM 不使用・数値は決定論・捏造ゼロ)。

★データ検証で確定した「作れる YoY / 作れない YoY」の境界 (= 捏造防止の核心):
- **既存店前年比 = OWNDAYS 本部の公式値のみ信頼**: Monday Dash (`owndays-monday-dash-latest.md`) に
  単日 + MTD の 既存店前年比 (曜日対比 & 同日対比、売上/客数/客単価) が canonical。最重要 KPI。
  → これを surface する (既存店・客数・客単価 の昨年対比はここで答わる)。
- **全店前年比 = 完了月のみ monthly.json で算出可**: `monthly.json` に月次全社 {JPYAmount, CustomerCount}
  が 2023〜 揃う。**完了した月同士** (当年 vs 前年同月) なら 全店 売上/客数/客単価 YoY を決定論算出。
- **日次の既存店売上前年比 = 店舗別 API から算出可 (★2026-07-20 追加調査で判明)**:
  `data/brain/import/owndays_mobile_api_storelist_{YYYY-MM-DD}.json` (mobile scraper が単日
  startDate=endDate=today で取得) が店舗ごとに当日 Amount(現地通貨) + 前年 yAmount + DollarRate を持つ。
  既存店 = **Amount>0 かつ yAmount>0** の店 (前年実績 yAmount がある店を母集団に、当年売上のある店を
  日次で集計する近似。yAmount の母集団は本部由来だが日次 a>0 フィルタは自前)。
  ★**当日 (today) は集計途中 = 除外**する (partial Amount ÷ 前年終日 = 壊れた比率になるため)。
  **単一通貨の国 (日本/台湾/…) は DollarShort で filter すれば為替歪みゼロで正確** (日本の日次既存店売上
  前年比が nation-daily 合計と完全一致することを実証)。★**全社を聞かれたら為替換算の概算1値でなく国別内訳
  を並べる** (海山指示 2026-07-20、各国=単一通貨=正確)。**客単価は既存店でなく全店の実額** (this-year、
  当日 全店売上÷全店客数) を表示 (海山指示)。**既存店の客数/客単価前年比は日次で不可** (API が前年客数
  yCustomerCount を返さない)。客単価の前年比は完了月 or 本部週次/MTD 公式で。
- **算出しない (社内に確定データが無い)**:
  ① 日次の全店前年比 (totaldaily.json は昨年の日次が疎)。② 日次の既存店 客数/客単価 前年比
  (前年客数が日次で無い)。③ 当月 MTD の自前 全店/既存店 YoY (monthly_stores.yAmount は前年フル月で
     当月 MTD と粒度不一致 = 自前計算 85% vs 公式 123% の乖離を実証)。→ 正直に「無い」と返す。
"""
from __future__ import annotations

import json
import os
import re
from datetime import date, timedelta
from pathlib import Path

_YOY_RE = re.compile(r"前年比|前年同|昨年対比|昨年比|対前年|昨対|前年割れ|ＹｏＹ|YoY|yoy|既存店")
# 「YYYY年M月」「M月」「先月」「今月」等の月次指定 (完了月 全店 YoY 用)
_MONTH_RE = re.compile(r"(\d{4})?\s*年?\s*(\d{1,2})\s*月")


def detect_yoy_intent(query: str) -> bool:
    return bool(_YOY_RE.search(query or ""))


def _app_root() -> Path:
    return Path(os.getenv("BRAIN_APP_ROOT", "/app"))


def _import_dir(import_dir: Path | None) -> Path:
    return import_dir or (_app_root() / "data" / "brain" / "import" / "owndays_history")


def _num(x) -> int:
    try:
        return int(str(x).replace(",", ""))
    except (ValueError, TypeError):
        return 0


# ─── 日次の既存店売上前年比: 店舗別 API (yAmount) から算出 ───
# 通貨 → 国名 (単一通貨 filter = 為替歪みゼロで正確)。DollarShort でマッチ。
_CURRENCY_NATION = {
    "JPY": "日本", "TWD": "台湾", "SGD": "シンガポール", "THB": "タイ", "HKD": "香港",
    "PHP": "フィリピン", "MYR": "マレーシア", "KHR": "カンボジア", "AUD": "オーストラリア",
    "VND": "ベトナム", "IDR": "インドネシア", "INR": "インド",
}
_NATION_CURRENCY = {v: k for k, v in _CURRENCY_NATION.items()}

# 日本エリア名 (areatotal の AreaName、関東A/B・西日本A/B・九州A/B・中部・沖縄・東日本 等)。
# ★cross-check: エリア接尾を必須にしない (prefix 一致) = 「中部エリア」も「中部」も拾う。海外
# エリア (TH West/Central/桃新區/オーストラリア) はこの prefix で始まらない。rollup 合算行
# (関東エリア 等、当年 Amount=0) は呼び手が Amount>0 で除外する。
_JP_AREA_RE = re.compile(r"^(関東|関西|近畿|西日本|東日本|中部|東海|九州|沖縄|東北|北海道|中国|四国)")


def _load_storelist_json(sdir: Path, kind: str, d: date) -> list | None:
    p = sdir / f"owndays_mobile_api_{kind}_{d.isoformat()}.json"
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
    except Exception:
        return None
    return data if isinstance(data, list) else None


def build_japan_trend(query: str, *, today: date | None = None,
                      import_dir: Path | None = None) -> str | None:
    """★2026-07-20 海山「日本の売上が弱い、エリア別の昨年対比で趨勢を掴みやすく」:
    日本 (default/明示) の日次売上照会に、日本 total 前年比 + エリア別 前年比 を厚める。
    前年比は areatotal/nationtotal API の集計 (achievement = Amount/yAmount、OWNDAYS 自身の値)。
    当日 (集計途中) は除外。全社/他国/非売上/日付なしは None (発火しない)。"""
    from brain_wiki_helpers.daily_history_inject import detect_dimension, resolve_range, _SALES_RE
    if not _SALES_RE.search(query or ""):
        return None
    # ★cross-check: 他国明示 (台湾/香港 等) は日本趨勢を出さない (エリア次元でも)。
    nation_in_q = _detect_nation(query)
    if nation_in_q and nation_in_q != "日本":
        return None
    dim = detect_dimension(query)  # 全社明示は None → 日本趨勢は出さない
    japan_scope = dim in (("nation", ["日本"]),) or (dim and dim[0] == "area")
    if not japan_scope:
        return None
    today = today or date.today()
    rng = resolve_range(query, today)
    if not rng:
        return None
    dates, label = rng
    complete = [d for d in dates if d < today]
    running = len(complete) < len(dates)
    if not complete:
        return None  # 当日のみ → 趨勢は出さない (集計途中)
    sdir = _storelist_dir(import_dir)

    jp_t = jp_y = 0.0
    areas: dict[str, list[float]] = {}
    found = 0
    for d in complete:
        nt = _load_storelist_json(sdir, "nationtotal", d)
        at = _load_storelist_json(sdir, "areatotal", d)
        if nt is None and at is None:
            continue
        found += 1
        for r in nt or []:
            if "日本" in str(r.get("NationName", "")) and _fnum(r.get("Amount")) > 0:
                jp_t += _fnum(r.get("Amount")); jp_y += _fnum(r.get("yAmount"))
        for r in at or []:
            nm = str(r.get("AreaName", ""))
            a_amt, a_y = _fnum(r.get("Amount")), _fnum(r.get("yAmount"))
            # ★cross-check: rollup 合算行 (当年 Amount=0) は除外 (「関東 0%」漏れ防止)
            if _JP_AREA_RE.match(nm) and a_amt > 0 and a_y > 0:
                a = areas.setdefault(nm, [0.0, 0.0])
                a[0] += a_amt; a[1] += a_y
    if found == 0 or (jp_y <= 0 and not areas):
        return None
    partial = ""
    if found < len(complete) or running:
        partial = (f"・データがある {found}/{len(complete)} 日分の部分値"
                   if found < len(complete) else "") + ("・当日は集計途中で除外" if running else "")
    lines = [f"◆日本の趨勢 ({label}{partial}・店舗別API のエリア/国集計 achievement=全店ベース):"]
    if jp_y > 0:
        lines.append(f"- 日本 全店 売上前年比: {jp_t / jp_y * 100:.1f}%")
    if areas:
        ranked = sorted(areas.items(), key=lambda kv: -(kv[1][0] / kv[1][1]))
        parts = [f"{nm.replace('エリア', '')} {v[0] / v[1] * 100:.0f}%" for nm, v in ranked]
        lines.append("- エリア別 売上前年比 (高い順): " + " / ".join(parts))
    lines.append("- ※これは日次の 全店ベース 前年比 (OWNDAYS 国別/エリア achievement)。"
                 "既存店の前年比 (Monday Dash 週次/MTD 公式) とは母集団も期間も別で、混同・実額への貼付をしない。"
                 "客数/客単価の日次前年比は無し (売上のみ)。全社・海外は聞かれた時のみ。")
    return "\n".join(lines)


def _storelist_dir(import_dir: Path | None) -> Path:
    # storelist JSON は import/ 直下 (owndays_history サブフォルダではない)
    base = import_dir or (_app_root() / "data" / "brain" / "import")
    # import_dir が owndays_history を指してたら親を使う
    if base.name == "owndays_history":
        base = base.parent
    return base


def _fnum(x) -> float:
    try:
        return float(str(x).replace(",", ""))
    except (ValueError, TypeError):
        return 0.0


def _detect_nation(query: str) -> str | None:
    for nation in _NATION_CURRENCY:
        if nation in (query or ""):
            return nation
    return None


_AREA_TOKENS_YOY = ("関東", "関西", "九州", "中部", "東海", "北海道", "東北", "沖縄",
                    "中国", "四国", "エリア", "地域", "リーグ", "業態")


def _daily_existing_store_block(query: str, today: date, import_dir: Path | None) -> str | None:
    """日次の既存店売上前年比を店舗別 API (yAmount) から算出。単日/範囲対応。
    単一通貨の国指定は正確、全社は当日レート換算の概算。非該当/データ無しは None。

    ★cross-check critical 対策: 当日 (today) は集計途中 (Amount が部分値) で前年 (終日) と割ると
    壊れた % になるため**集計から除外**。当日のみのクエリは % を出さず「途中」と正直に返す。"""
    if "既存店" not in (query or ""):
        return None
    # エリア/業態/リーグは日次店舗別では正確に切れない (国別のみ) → 本部公式へ委ねる
    if _detect_nation(query) is None and any(t in query for t in _AREA_TOKENS_YOY):
        return ("◆日次の既存店売上前年比: エリア/業態/リーグ別の日次既存店は算出不可 "
                "(店舗別APIは国単位でのみ正確)。既存店前年比は本部の週次/MTD 公式、"
                "または国別 (例: 日本) で聞けば日次で正確に出せる。")
    from brain_wiki_helpers.daily_history_inject import resolve_range
    rng = resolve_range(query, today)
    if not rng:  # 日付が無い = 日次でない → Monday Dash 公式へ委ねる
        return None
    dates, label = rng
    if len(dates) > 14:
        return None
    # ★当日 (集計途中) を除外
    complete_dates = [d for d in dates if d < today]
    running_excluded = len(complete_dates) < len(dates)
    if not complete_dates:
        return ("◆日次の既存店売上前年比: 本日 (当日) は売上が集計途中で、前年 (終日) と割ると"
                "壊れた比率になるため出さない。確定するのは翌日以降。昨日や先週なら日次で出せる。")

    nation = _detect_nation(query)
    only_cur = _NATION_CURRENCY.get(nation) if nation else None
    sdir = _storelist_dir(import_dir)

    # 通貨 (=国) ごとに集計。既存店(yAmount>0)売上は現地通貨で比を取れば正確。
    # 全店客単価は全店(Amount>0)を対象に売上(JPY換算)/客数。
    from collections import defaultdict
    agg: dict[str, dict] = defaultdict(lambda: {"ex_t": 0.0, "ex_y": 0.0, "st": set(),
                                                "all_jpy": 0.0, "cust": 0})
    found_days = 0
    for d in complete_dates:
        p = sdir / f"owndays_mobile_api_storelist_{d.isoformat()}.json"
        try:
            rows = json.loads(p.read_text(encoding="utf-8"))
        except Exception:
            continue
        if not isinstance(rows, list):
            continue
        found_days += 1
        for r in rows:
            c = str(r.get("DollarShort"))
            if only_cur and c != only_cur:
                continue
            rate = _fnum(r.get("DollarRate"))
            a = _fnum(r.get("Amount"))
            if a <= 0 or rate <= 0:  # 全店/既存店とも不正レート・無売上は除外
                continue
            y, cu = _fnum(r.get("yAmount")), _fnum(r.get("CustomerCount"))
            g = agg[c]
            g["all_jpy"] += a * rate
            g["cust"] += cu
            if y > 0:  # 既存店 = 当年も前年も売上あり
                g["ex_t"] += a
                g["ex_y"] += y
                g["st"].add(r.get("StoreNo"))
    if found_days == 0:
        return (f"◆日次の既存店売上前年比 ({label}): 店舗別データが手元に無い "
                "(保持期間外)。推測で作らない。")
    nations = [(c, g) for c, g in agg.items() if g["ex_y"] > 0]
    if not nations:
        return (f"◆日次の既存店売上前年比 ({label}): 該当期間は前年実績のある店 (既存店) が "
                "データ上見当たらず算出不可。推測で作らない。")
    nations.sort(key=lambda kv: -kv[1]["all_jpy"])
    partial = ""
    if found_days < len(complete_dates) or running_excluded:
        partial = (f" ※データがある {found_days} 日分のみの部分合計"
                   + ("・当日は集計途中で除外" if running_excluded else "") + " (期間全体ではない)")

    def _uprice(g):
        return round(g["all_jpy"] / g["cust"]) if g["cust"] else 0

    if only_cur:  # 単一国 = 正確
        g = agg[only_cur]
        pct = g["ex_t"] / g["ex_y"] * 100
        lines = [
            f"◆日次 既存店売上 前年比 ({nation}・{label}・店舗別API yAmount ベース){partial}:",
            f"- 既存店 売上前年比: {pct:.1f}%  (当年 {g['ex_t']:,.0f}円 / 前年 {g['ex_y']:,.0f}円、既存店 {len(g['st'])}店)",
            f"- 全店 客単価 (当日実額・参考): {_uprice(g):,}円  ※客単価は全店ベースの実額。"
            "客単価の前年比は完了月 or 本部週次/MTD公式で見る (日次の既存店客単価前年比は前年客数が無く不可)。",
            "- ※既存店 = 前年も当年も売上がある店 (本部の前年実績 yAmount 基準)。"
            "前年基準は本部API yAmount 定義 (曜日/同日対比は本部公式)。",
        ]
        return "\n".join(lines)

    # 全社 → 国別内訳 (各国は単一通貨=正確。為替混在の全社合算は出さない)
    lines = [f"◆日次 既存店売上 前年比 (国別・{label}・店舗別API yAmount ベース) "
             f"※全社は通貨混在のため合算せず国別で表示{partial}:"]
    for c, g in nations[:10]:
        nm = _CURRENCY_NATION.get(c, c + "圏")
        lines.append(f"- {nm}: 売上 {g['ex_t'] / g['ex_y'] * 100:.1f}% ({len(g['st'])}店) / 全店客単価 {_uprice(g):,}円")
    lines.append("（各国は単一通貨=正確。全社を1つの%に合算すると為替方式が本部と異なるため出さない）")
    lines.append("※客単価は全店ベースの実額 (既存店客単価の前年比は前年客数が無く日次不可)。"
                 "客数/客単価の既存店前年比は本部週次/MTD公式を見る。")
    return "\n".join(lines)


# ─── 既存店前年比: Monday Dash 公式 (最重要 KPI) ───

def _strip_md(s: str) -> str:
    return s.replace("**", "").replace("★", "").strip()


_FM_UPDATED_RE = re.compile(r"^(?:updated|last_updated|date)\s*:\s*(\d{4}-\d{2}-\d{2})", re.MULTILINE)


def _asof_note(md: str, today: date) -> str:
    """frontmatter の updated 日から as-of ラベルを作る。stale なら警告 (★DA HIGH: 古い値を
    最新と誤認させない = CEO の YoY 判断を stale データで誤らせないため)。"""
    m = _FM_UPDATED_RE.search(md[:600])
    if not m:
        return "（データ基準日: 不明。鮮度を確認のこと）"
    try:
        upd = date.fromisoformat(m.group(1))
    except ValueError:
        return "（データ基準日: 不明）"
    age = (today - upd).days
    warn = "  ⚠ Monday Dash が古い可能性 (更新確認を)" if age >= 10 else ""
    return f"（データ基準日: {upd.isoformat()} / {age}日前{warn}）"


def _existing_store_block(knowledge_dir: Path | None, today: date) -> str | None:
    """Monday Dash の『### ★ MTD まとめ』section (= 既存店前年比 canonical、期間ラベル付き)
    をそのまま抽出。生の LINE Works dump 行や古い個別例は拾わない。"""
    kd = knowledge_dir or (_app_root() / "data" / "brain" / "wiki" / "knowledge")
    try:
        md = (kd / "owndays-monday-dash-latest.md").read_text(encoding="utf-8")
    except Exception:
        return None
    lines = md.splitlines()
    # 「### ★ MTD まとめ」から次の見出し (### / ## / **注**) までを取る
    start = next((i for i, ln in enumerate(lines) if ln.startswith("### ") and "MTD まとめ" in ln), None)
    if start is None:
        return None
    body = []
    for ln in lines[start + 1:]:
        s = ln.strip()
        if s.startswith(("### ", "## ")) or s.startswith("**注"):
            break
        if not s:
            continue
        # 全店売上 / 既存店前年比 / 期間サブ見出し (月間累計/直近単日) のみ採用
        if ("既存店前年比" in s or "全店売上" in s
                or s.startswith("**月") or s.startswith("**直近") or "累計" in s or "単日" in s):
            body.append(_strip_md(s.lstrip("- ")))
    if not any("既存店前年比" in b for b in body):
        return None
    # ★DA HIGH 強化: frontmatter の build 日でなく、中身の「M月D日」からデータ鮮度を判定。
    #   build_monday_dash は LINE Works 本文が古くても生成日を更新するため、本文の日付が真の as-of。
    content_note = _content_staleness(body, today)
    return ("◆既存店前年比 (OWNDAYS 本部 公式 = 最重要 KPI。Monday Dash の月間累計 MTD + 直近単日) "
            + content_note + ":\n"
            + "\n".join(f"  {b}" for b in body)
            + "\n※既存店前年比は本部算出の週次/MTD が公式。日次の既存店前年比は社内に確定データが無い。")


_MD_DATE_RE = re.compile(r"(\d{1,2})\s*月\s*(\d{1,2})\s*日")


def _content_staleness(body: list[str], today: date) -> str:
    """既存店ブロック本文の最新「M月D日」から実データ鮮度を出す (frontmatter build 日でなく)。"""
    best = None
    for b in body:
        for mo, dy in _MD_DATE_RE.findall(b):
            mo, dy = int(mo), int(dy)
            try:
                d = date(today.year, mo, dy)
            except ValueError:
                continue
            if d > today:  # 未来日 = 前年扱い
                d = date(today.year - 1, mo, dy)
            if best is None or d > best:
                best = d
    if best is None:
        return _asof_note("", today)  # 日付が読めない → 不明
    age = (today - best).days
    warn = "  ⚠ このデータは古い可能性 (Monday Dash 未更新)。本部に最新を確認" if age >= 10 else ""
    return f"（データ基準日: {best.isoformat()} / {age}日前{warn}）"


# ─── 全店前年比: 完了月のみ monthly.json ───

def _resolve_month(query: str, today: date) -> tuple[str, str] | None:
    """query から (当年YYYY-MM, 前年YYYY-MM) を解決。完了月のみ。当月/未完了は None。"""
    q = query or ""
    if "先月" in q:
        m = today.month - 1 or 12
        y = today.year if today.month > 1 else today.year - 1
        return f"{y:04d}-{m:02d}", f"{y - 1:04d}-{m:02d}"
    mm = _MONTH_RE.search(q)
    if mm:
        yr = int(mm.group(1)) if mm.group(1) else today.year
        mo = int(mm.group(2))
        if not (1 <= mo <= 12):
            return None
        # 当月 (進行中) は完了月でないので除外
        if yr == today.year and mo == today.month:
            return None
        if (yr, mo) > (today.year, today.month):  # 未来指定は前年扱い
            yr -= 1
        return f"{yr:04d}-{mo:02d}", f"{yr - 1:04d}-{mo:02d}"
    return None


def _last_day_of_month(ym: str) -> str:
    """YYYY-MM の月末日 (YYYY-MM-DD)。"""
    y, m = int(ym[:4]), int(ym[5:7])
    nxt = date(y + (m == 12), (m % 12) + 1, 1)
    return (nxt - timedelta(days=1)).isoformat()


def _allstore_month_block(query: str, today: date, import_dir: Path | None) -> str | None:
    mo = _resolve_month(query, today)
    if not mo:
        return None
    cur_key, prev_key = mo
    p = _import_dir(import_dir) / "monthly.json"
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
    except Exception:
        return None
    if not isinstance(data, dict):
        return None
    # ★DA MEDIUM: end が月末でない = partial 月 (取込途中 / 月境界の refresh 窓) は完了扱いしない。
    # partial を「完了月・確定値」として full 前年月と突合すると過小 YoY を definitive に出す事故。
    cur_entry = data.get(cur_key) or {}
    cur_end = str(cur_entry.get("end") or "")
    if cur_end and cur_end < _last_day_of_month(cur_key):
        return None

    def _tot(k):
        t = (data.get(k) or {}).get("total") or {}
        s, c = _num(t.get("JPYAmount")), _num(t.get("CustomerCount"))
        return (s, c) if s and c else None
    cur, prev = _tot(cur_key), _tot(prev_key)
    if not cur:
        return None
    cs, cc = cur
    cu = round(cs / cc) if cc else 0
    lines = [f"◆全店 前年比 (月次・完了月 {cur_key}、monthly.json 確定値):",
             f"- 当年 {cur_key}: 売上 {cs:,}円 / 客数 {cc:,} / 客単価 {cu:,}円"]
    if prev:
        ps, pc = prev
        pu = round(ps / pc) if pc else 0
        lines.append(
            f"- 前年 {prev_key} 比: 売上 {cs / ps * 100:.0f}% / 客数 {cc / pc * 100:.0f}% / "
            f"客単価 {(cu / pu * 100 if pu else 0):.0f}%  (前年 売上 {ps:,}円 / 客数 {pc:,})")
    else:
        lines.append(f"- 前年 {prev_key} の月次データが無く、全店前年比は算出不可 (推測で作らない)。")
    return "\n".join(lines)


def build_yoy_context(query: str, *, today: date | None = None,
                      import_dir: Path | None = None,
                      knowledge_dir: Path | None = None) -> str | None:
    """YoY 意図の質問に昨年対比の決定論 block を返す。非該当は None。"""
    if not detect_yoy_intent(query or ""):
        return None
    today = today or date.today()
    daily_exist = _daily_existing_store_block(query, today, import_dir)  # 日次の既存店売上 YoY
    exist = _existing_store_block(knowledge_dir, today)                  # 本部公式 (週次/MTD)
    allstore = _allstore_month_block(query, today, import_dir)           # 完了月 全店

    if not daily_exist and not exist and not allstore:
        return ("【参照データ: 昨年対比 (YoY)】\n"
                "⚠ 昨年対比の確定データ (Monday Dash 既存店前年比 / 完了月の全店 monthly / 日次店舗別) が"
                "手元に無い。推測で前年比を作らず「昨年対比の確定データが手元に無い」と正直に伝える。")

    parts = ["【参照データ: 昨年対比 (YoY) — 確定値・決定論】"]
    # 日次の既存店を聞かれたら日次ブロックを先頭に (本部公式は週次で参考として続ける)
    if daily_exist:
        parts.append(daily_exist)
        if exist:
            parts.append("（参考: 本部公式の既存店前年比は週次/MTD。客数・客単価はこちら↓）")
    ordered = []
    if "全店" in (query or "") and allstore:
        ordered = [allstore] + ([exist] if exist else [])
    else:
        ordered = ([exist] if exist else []) + ([allstore] if allstore else [])
    parts += ordered

    # ★cross-check: daily_exist の有無で末尾指示を出し分け (自己矛盾を避ける)。
    if daily_exist:
        parts.append(
            "【指示】前年比・昨年対比の数値は必ずこのブロックの決定論値をそのまま使い、推測しない。"
            "◆日次の既存店売上前年比は上の店舗別APIブロックが正 (当日は集計途中で除外済)。"
            "客数/客単価の既存店前年比と全店前年比の日次は無く、既存店の客数/客単価は本部週次/MTD公式、"
            "全店前年比は完了月で見る。既存店前年比(%)を日次の実額(円)にそのまま貼り付けない。")
    else:
        parts.append(
            "【指示】前年比・昨年対比の数値は必ずこのブロックの決定論値をそのまま使い、"
            "ブロックに無い比率を計算・推測しない。"
            "★既存店前年比(%)は全社・既存店の週次/MTD であり、日次や国別・エリア別の実額(円)とは"
            "母集団も期間も別。日次/国別の実額に既存店前年比(%)を貼り付けない(別項目として示す)。"
            "◆日次の全店前年比 は社内に確定データが無い。既存店の日次売上前年比は国別で店舗別APIから"
            "出せるが、それ以外の日次前年比 (全店・客数・客単価) は本部の週次/MTD公式または完了月で見る。")
    return "\n".join(parts)
