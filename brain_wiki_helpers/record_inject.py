"""最上級クエリ (「過去最高」「一番売れた日」「最低の月」) の決定論注入。

★2026-08-16 海山「シンガポールの過去3年の最高売上は?」に誤答した件の根治。

何が起きたか:
  「過去最高」には期間語が無いので `daily_history_inject.resolve_range()` が None を
  返し、決定論注入が **一度も発火しなかった**。その結果 bot が見たのは wiki が持つ
  日次全体ではなく **ベクトル検索が返した数チャンクだけ**で、その断片の中の最大値を
  「最高」と答えた。実際には同じ wiki 内にもっと高い日があり (2026-08-10 > 2026-08-08)、
  bot は「確認できている範囲では」と正しく留保しながら、その"範囲"の集計自体を
  間違えていた。最大値・順位の算出を LLM の読み取りに任せていたことが故障の本体で、
  データを 3 年に増やしても直らない。ここで決定論的に算出して注入する。

設計 (daily_history_inject / yoy_inject と同じ契約):
  - 入力の部分集合しか返さない = 捏造ゼロ。為替換算もしない
  - **粒度と単位を必ず分けて出す**。月次 (国別=現地通貨) と日次 (円) を混ぜると
    「7,654,321 < 47,777,777 だから月次より単日が大きい」型の誤読を招く
  - 保持範囲・収集開始日は実データから導く (ハードコードしない)

★§1.15(b) cross-check 3 体の指摘を反映 (初版は 3 体とも NO-GO):
  - 収集開始日のハードコードが実データ (2026-05-25) と食い違い、同じブロック内で
    自己矛盾していた → 実 rows から導出
  - 営業していない国 (ベトナム等) で「最高 0 円」を断定していた → 0/0 行を除外
  - 「最も売れていない日」で _MAX_RE が先に当たり最高を「最低」として返していた
    → 最上級語の直後の否定を見て向きを反転
  - 客数/客単価を聞かれても売上でランキングしていた → メトリック判定を追加
  - 当日 (集計途中) を日次ランキングに混ぜていた (月次は当月除外なのに非対称)
  - 「タイ」がタイミング/ネクタイに部分一致していた → カナ境界判定
  - 複数国クエリで NATION_TOKENS の宣言順に先頭 1 国だけ出していた → 出現順・最大 2 国
"""
from __future__ import annotations

import json
import re
from datetime import date
from pathlib import Path

from .daily_history_inject import NATION_TOKENS, _ALLCO_RE, _JAPAN_NATION_RE, resolve_range

# ── 最上級の向き ──────────────────────────────────────────────────────────
_MIN_RE = re.compile(r"最低|最小|最少|ワースト|worst|lowest", re.IGNORECASE)
_MAX_RE = re.compile(
    r"最高|最大|最多|最も|もっとも|一番|トップ|ベスト|過去最|ピーク(?!時|タイム)|"
    r"最高記録|記録更新|best|peak|highest|max",
    re.IGNORECASE,
)
# 最上級語の直後にこれが来たら向きが反転する (「最も売れていない日」「一番少なかった月」)
_NEG_AFTER_RE = re.compile(r"な(い|かった)|ません|低[いかく]|少な|悪[いか]|振るわ|伸びな")

# ── メトリック (順に判定。客単価は「単価」を含むので先に見る) ──────────────
_METRICS: tuple[tuple[str, re.Pattern, str], ...] = (
    ("unit_price", re.compile(r"客単価|単価"), "客単価"),
    ("customers", re.compile(r"客数|来客|客足|人数"), "客数"),
    ("sales", re.compile(r"売り?上げ?|売上|売れ|日商|月商|sales|revenue", re.I), "売上"),
)
# 売上系の語が無ければ発火しない (「最高のチーム」除け)
_ANY_METRIC_RE = re.compile("|".join(p.pattern for _k, p, _l in _METRICS), re.IGNORECASE)
# 国合計より細かい粒度を聞いている = この注入の担当外
_FINER_RE = re.compile(r"商品|品番|フレーム|レンズ|SKU|型番|店舗|店別|支店|[^\s]店\b")

_KANA_RE = re.compile(r"[ァ-ヶー]")
_MONTHLY_FILE = "owndays-history-monthly.md"
_NATION_DAILY_FILE = "owndays-history-nationdaily.md"
_TOTAL_DAILY_FILE = "owndays-history-totaldaily.md"

# 月次 (国別): | 2025-12 | 7,654,321 SGD | 38,629 |
_NATION_MONTH_RE = re.compile(
    r"^\|\s*(?P<ym>\d{4}-\d{2})\s*\|\s*(?P<amt>[\d,]+)\s*(?P<cur>[A-Z]{3})?\s*\|\s*(?P<cust>[\d,]+)\s*\|",
    re.MULTILINE)
# 月次 (全体): | 2023-08 | 2,111,222,333円 | 224,857 | ... |
_TOTAL_MONTH_RE = re.compile(
    r"^\|\s*(?P<ym>\d{4}-\d{2})\s*\|\s*(?P<amt>[\d,]+)円\s*\|\s*(?P<cust>[\d,]+)\s*\|",
    re.MULTILINE)
# 日次 (国別、5列/7列 両対応): | 2 | シンガポール | 1,648 | 41,111,111 | ...
_NATION_DAY_RE = re.compile(
    r"^\|\s*\d+\s*\|\s*(?P<name>[^\|]+?)\s*\|\s*(?P<cust>[\d,]+)\s*\|\s*(?P<sales>[\d,]+)\s*\|",
    re.MULTILINE)
# 日次 (全社): | 2023-08-10 | (木) | 71,111,222円 | 6,093 |
_TOTAL_DAY_RE = re.compile(
    r"^\|\s*(?P<ymd>\d{4}-\d{2}-\d{2})\s*\|[^|]*\|\s*(?P<sales>[\d,]+)円\s*\|\s*(?P<cust>[\d,]+)\s*\|",
    re.MULTILINE)


def _int(s: str) -> int:
    return int(s.replace(",", ""))


def _direction(q: str) -> bool | None:
    """True=最小 / False=最大 / None=最上級でない。"""
    if _MIN_RE.search(q):
        return True
    m = _MAX_RE.search(q)
    if not m:
        return None
    # 「最も売れていない」「一番少なかった」= 最上級 + 否定 → 最小
    return bool(_NEG_AFTER_RE.search(q[m.end(): m.end() + 10]))


def _metric(q: str) -> tuple[str, str]:
    for key, pat, label in _METRICS:
        if pat.search(q):
            return key, label
    return "sales", "売上"


def _nations_in(q: str) -> list[str]:
    """出現順の国名。カナ語の部分一致 (タイミング/ネクタイ) を弾く。"""
    hits: list[tuple[int, str]] = []
    for t in NATION_TOKENS:
        if t == "日本":
            continue
        for m in re.finditer(re.escape(t), q):
            before = q[m.start() - 1] if m.start() else ""
            after = q[m.end()] if m.end() < len(q) else ""
            if _KANA_RE.fullmatch(t[0] or ""):        # カナ国名は前後がカナなら別語
                if _KANA_RE.fullmatch(before or "") or _KANA_RE.fullmatch(after or ""):
                    continue
            elif t.isascii():                          # UAE 等
                if (before or "x").isalnum() or (after or "x").isalnum():
                    continue
            hits.append((m.start(), t))
            break
    jm = _JAPAN_NATION_RE.search(q)
    if jm:
        hits.append((jm.start(), "日本"))
    hits.sort()
    out: list[str] = []
    for _pos, t in hits:
        if t not in out:
            out.append(t)
    return out


def detect_record_query(query: str) -> dict | None:
    """最上級クエリなら {scope, nations, want_min, metric, metric_label} を返す。"""
    q = query or ""
    want_min = _direction(q)
    if want_min is None or not _ANY_METRIC_RE.search(q) or _FINER_RE.search(q):
        return None
    metric, metric_label = _metric(q)
    if _ALLCO_RE.search(q):
        return {"scope": "company", "nations": [], "want_min": want_min,
                "metric": metric, "metric_label": metric_label}
    nations = _nations_in(q)
    if not nations:            # 国が明示されていない → 従来経路 (日本 default にしない)
        return None
    return {"scope": "nation", "nations": nations[:2], "want_min": want_min,
            "metric": metric, "metric_label": metric_label}


# ── データ読み出し (返り値は [(key, sales, customers)]、単位は呼び出し側が持つ) ──

def _drop_empty(rows: list[tuple]) -> list[tuple]:
    """売上も客数も 0 の行を捨てる (未出店・休業を「記録」にしない)。"""
    return [r for r in rows if not (r[1] == 0 and r[2] == 0)]


def nation_monthly(name: str, kd: Path) -> tuple[list[tuple], str] | None:
    try:
        md = (kd / _MONTHLY_FILE).read_text(encoding="utf-8")
    except OSError:
        return None
    m = re.search(rf"^###\s*{re.escape(name)}\s*$", md, re.MULTILINE)
    if not m:
        return None
    rest = md[m.end():]
    nxt = re.search(r"^#{2,3}\s", rest, re.MULTILINE)
    section = rest[: nxt.start()] if nxt else rest
    rows, currency = [], ""
    for rm in _NATION_MONTH_RE.finditer(section):
        rows.append((rm.group("ym"), _int(rm.group("amt")), _int(rm.group("cust"))))
        if not currency and rm.group("cur"):
            currency = rm.group("cur")
    rows = _drop_empty(sorted(rows))
    # 通貨が読めない時は JPY と決めつけない (誤った単位の断定を避ける)
    return (rows, currency) if rows else None


def company_monthly(kd: Path) -> tuple[list[tuple], str] | None:
    try:
        md = (kd / _MONTHLY_FILE).read_text(encoding="utf-8")
    except OSError:
        return None
    m = re.search(r"^##\s*全体月次推移\s*$", md, re.MULTILINE)
    if not m:
        return None
    rest = md[m.end():]
    nxt = re.search(r"^##\s", rest, re.MULTILINE)
    section = rest[: nxt.start()] if nxt else rest
    rows = _drop_empty(sorted(
        (rm.group("ym"), _int(rm.group("amt")), _int(rm.group("cust")))
        for rm in _TOTAL_MONTH_RE.finditer(section)))
    return (rows, "JPY") if rows else None


def _backfill_dir(kd: Path) -> Path:
    """3 年 backfill の JSON cache (data/brain/import/owndays_history/nationdaily/)。

    wiki には流さない (1,102 日 × 約20カ国 ≈ 1MB は現行最大 storesdaily の倍で、
    2026-07-26 の索引肥大による全断と同じ形になる)。決定論注入からだけ読む。
    """
    return kd.parent.parent / "import" / "owndays_history" / "nationdaily"


def nation_daily_backfill(name: str, kd: Path) -> list[tuple]:
    """backfill cache から [(ymd, sales_jpy, customers)]。無ければ空。

    ★API が返すのは確定値で、当日 snapshot (wiki 側) より 0.3〜1.5% 高い
      (遅れて計上される取引。5 日で実測、方向は一貫して正)。混ぜると
      backfill 期間だけが有利になり「記録」が偏るので、**同じ日が両方に
      あるときは確定値を採る** = 系列全体を確定値ベースに寄せる。
    """
    d = _backfill_dir(kd)
    if not d.is_dir():
        return []
    rows: list[tuple] = []
    for f in d.glob("*.json"):
        ymd = f.stem
        if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", ymd):
            continue
        try:
            data = json.loads(f.read_text(encoding="utf-8"))
        except Exception:
            continue
        if not isinstance(data, list):
            continue
        for r in data:
            if not isinstance(r, dict) or (r.get("NationName") or "").strip() != name:
                continue
            try:
                sales = int(float(r.get("Amount") or 0) * float(r.get("DollarRate") or 0))
                rows.append((ymd, sales, int(r.get("CustomerCount") or 0)))
            except (TypeError, ValueError):
                pass
            break
    return rows


def nation_daily(name: str, kd: Path) -> list[tuple] | None:
    by_day: dict[str, tuple] = {}
    try:
        md = (kd / _NATION_DAILY_FILE).read_text(encoding="utf-8")
    except OSError:
        md = ""
    for chunk in md.split("\n## ")[1:]:
        dm = re.match(r"(\d{4}-\d{2}-\d{2})", chunk)
        if not dm:
            continue
        for rm in _NATION_DAY_RE.finditer(chunk):
            if rm.group("name").strip() == name:
                by_day[dm.group(1)] = (dm.group(1), _int(rm.group("sales")),
                                       _int(rm.group("cust")))
                break
    for row in nation_daily_backfill(name, kd):   # 確定値が snapshot を上書き
        by_day[row[0]] = row
    rows = _drop_empty(sorted(by_day.values()))
    return rows or None


def company_daily(kd: Path) -> list[tuple] | None:
    try:
        md = (kd / _TOTAL_DAILY_FILE).read_text(encoding="utf-8")
    except OSError:
        return None
    rows = _drop_empty(sorted(
        (rm.group("ymd"), _int(rm.group("sales")), _int(rm.group("cust")))
        for rm in _TOTAL_DAY_RE.finditer(md)))
    return rows or None


# ── ランキング ────────────────────────────────────────────────────────────

def _value(row: tuple, metric: str) -> int:
    _k, sales, cust = row
    if metric == "customers":
        return cust
    if metric == "unit_price":
        return sales // cust if cust else 0
    return sales


def _rank(rows: list[tuple], metric: str, want_min: bool, top: int = 3) -> list[tuple]:
    usable = [r for r in rows if _value(r, metric) > 0]
    return sorted(usable, key=lambda r: _value(r, metric), reverse=not want_min)[:top]


def _fmt(row: tuple, metric: str, unit: str) -> str:
    key, sales, cust = row
    val = _value(row, metric)
    if metric == "customers":
        return f"{key}  {val:,} 人  (売上 {sales:,} {unit})"
    if metric == "unit_price":
        return f"{key}  {val:,} {unit}  (客数 {cust:,})"
    return f"{key}  {val:,} {unit}  (客数 {cust:,})"


def _block_for(monthly, daily, *, det: dict, today: date,
               period: tuple[list[date], str] | None) -> tuple[list[str], set[str]]:
    """(本文行, 実際に使った単位) を返す。行が空なら実績なし。

    単位は **実際に出力した節のものだけ** を返す (期間指定で月次を出さない時に
    SGD を混在警告に数えてしまうと、存在しない矛盾を警告することになる)。
    """
    metric, mlabel, want_min = det["metric"], det["metric_label"], det["want_min"]
    sup = "最低" if want_min else "最高"
    out: list[str] = []
    units: set[str] = set()
    cur_ym = today.strftime("%Y-%m")
    today_s = today.isoformat()

    if monthly and not period:
        rows, currency = monthly
        unit = "円" if currency in ("JPY", "") else currency
        ranked = _rank([r for r in rows if r[0] != cur_ym], metric, want_min)
        if ranked:
            span = f"{rows[0][0]}〜{rows[-1][0]} の {len(rows)} ヶ月"
            out.append(f"■ 月次 ({span}、金額は {unit} 建て) — {mlabel}の{sup}:")
            out += [f"  {i}位 {_fmt(r, metric, unit)}" for i, r in enumerate(ranked, 1)]
            if any(r[0] == cur_ym for r in rows):
                out.append(f"  ※{cur_ym} は進行中のため順位比較から除外している")
            units.add(unit)

    if daily:
        rows = [r for r in daily if r[0] != today_s]
        if period:
            keep = {d.isoformat() for d in period[0]}
            rows = [r for r in rows if r[0] in keep]
        ranked = _rank(rows, metric, want_min)
        if ranked:
            scope = (f"{period[1]} に限定" if period
                     else f"{daily[0][0]}〜{daily[-1][0]} の {len(daily)} 日 **のみ保持**")
            out.append(f"■ 日次 ({scope}、金額は 円 建て) — {mlabel}の{sup}:")
            out += [f"  {i}位 {_fmt(r, metric, '円')}" for i, r in enumerate(ranked, 1)]
            out.append(f"  ※本日 {today_s} は集計途中のため順位比較から除外している")
            if not period:
                # 留保の強さを保持期間で変える。3 年分あるのに「断定するな」と言い続けると
                # 答えられる問いに答えない縮退になる (backfill 前の文面をそのまま残さない)。
                try:
                    covered = (today - date.fromisoformat(daily[0][0])).days
                except ValueError:
                    covered = 0
                if covered >= 3 * 365 - 30:
                    out.append(
                        f"  ○ {daily[0][0]} 以降 約{covered // 365} 年分を保持しているので、"
                        "「過去3年で最高」はこの範囲で断定してよい "
                        "(それ以前は存在しないので『過去最高』は範囲を添える)。")
                else:
                    out.append(
                        f"  ⚠ この日次履歴は {daily[0][0]} 以降しか保持していない。"
                        "それ以前の日は存在しないので「過去最高」「過去3年で最高」を"
                        "日次で断定してはいけない。必ず保持範囲を明示すること。")
            units.add("円")
    return out, units


def build_context(query: str, *, today: date | None = None,
                  knowledge_dir: Path | None = None) -> str | None:
    """注入ブロックを組み立てる。トリガー外・データ無しは None (通常フロー)。"""
    det = detect_record_query(query or "")
    if not det or knowledge_dir is None:
        return None
    kd = Path(knowledge_dir)
    today = today or date.today()
    # 期間語があれば日次をその範囲に絞る (「今月の最高日」型に窓全体の順位を出さない)
    period = resolve_range(query or "", today)

    # 参照先が 1 つも無い環境 (= 未配備) は静かに通常フローへ。
    # 「実績が無い」と言えるのは、履歴ファイルが在ってその国の行が無い時だけ。
    if not any((kd / f).exists() for f in
               (_MONTHLY_FILE, _NATION_DAILY_FILE, _TOTAL_DAILY_FILE)):
        return None

    sections: list[str] = []
    units: set[str] = set()
    if det["scope"] == "company":
        body, used = _block_for(company_monthly(kd), company_daily(kd),
                                det=det, today=today, period=period)
        if body:
            sections.append("【参照データ: 全社の売上記録 (決定論集計 — この値が正)】")
            sections += body
            units |= used
    else:
        for name in det["nations"]:
            body, used = _block_for(nation_monthly(name, kd), nation_daily(name, kd),
                                    det=det, today=today, period=period)
            if not body:
                sections.append(
                    f"【{name}】保持範囲内に実績が無い (未出店・未取得の可能性)。"
                    "売上ゼロを『記録』として答えないこと。")
                continue
            sections.append(f"【参照データ: {name} の売上記録 (決定論集計 — この値が正)】")
            sections += body
            units |= used

    if not sections:
        return None

    tail = ["【指示】順位・最大値・最小値は必ずこのブロックの値をそのまま使い、"
            "表から自分で探し直さない (断片だけを見て『最高』と答える誤りの原因)。"
            "月次と日次は粒度が違うので必ず区別し、日次に無いことを理由に"
            "『履歴が無い』と一般化しない。"]
    if len(units) > 1:
        tail.append(
            f"⚠ 単位が混在している ({' / '.join(sorted(units))})。"
            "月次と日次の数値を直接比較・合算してはいけない。"
            "金額は必ず通貨を付けて答え、為替換算はしない。")
    if det["scope"] == "nation" and len(det["nations"]) < len(_nations_in(query or "")):
        tail.append("⚠ 質問に含まれる国のうち一部のみを載せている。"
                    "載っていない国は「このブロックには無い」と伝える。")
    return "\n".join(sections + tail)
