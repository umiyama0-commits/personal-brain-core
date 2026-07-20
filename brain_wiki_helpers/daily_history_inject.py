"""daily_history_inject — 日付範囲×売上質問への日次履歴の決定論注入 (★2026-07-13).

背景 (failure-log 2026-07-13): 「先週の関東エリアの売り上げ」に対し、クローンが retrieval で
取れた 4 日分は正しく使いつつ、chunk 窓から外れた両端 2 日 (7/07・7/12) を捏造値で埋め、
週計・A/B 内訳まで自前計算で誤答 (実際は B > A なのに A > B と回答)。どのデータにも存在
しない数字が「確からしい体裁」で返った。さらにその誤答が compile で wiki/decisions/ に
confidence: high 昇格 (二次汚染、compile_number_scrub が対の防御)。

防御 = tenpo lookup_service と同じ思想: **数字の網羅性をベクトル検索の当たり外れに委ねない**。
日付範囲 (先週/今週/昨日/M月D日…) × 次元 (エリア/国別/業態/リーグ) を決定論で解決し、
knowledge/ の日次履歴 wiki から該当日セクションを **原文そのまま** + 決定論集計付きで注入。
LLM の仕事は整形と所感だけに絞る。応答後は sales_numeric_guard が「注入に無い数値」を検知
して確定値を決定論追記 (桁事故ガードと同型、false positive でも追記されるのは正値 = 安全側)。

pure function 集 (LLM 不使用・I/O は knowledge_dir 読みのみ)。呼び手は brain_wiki.py
clone_respond_public の 1 箇所 (§1.12b)。日次履歴は build_breakdown_history.py が
毎 scrape サイクル再構築する確定ソース (§3.7)。
"""
from __future__ import annotations

import re
from datetime import date, timedelta
from pathlib import Path

# 次元 → 日次履歴 wiki (build_breakdown_history.py の出力と対応)
HISTORY_FILES = {
    "area": "owndays-history-areadaily.md",
    "nation": "owndays-history-nationdaily.md",
    "type": "owndays-history-typedaily.md",
    "league": "owndays-history-leaguedaily.md",
}

# エリア名 (areadaily の行名は「関東Aエリア」等 = 部分一致で拾う)
AREA_TOKENS = ("関東", "九州", "中部", "沖縄", "東日本", "西日本")
# 国名 (nation 次元の行フィルタ。「日本」は単独 trigger にしない = 「日本一」等の誤爆回避、
# 次元が確定した後のフィルタとしてのみ使う)
NATION_TOKENS = ("日本", "台湾", "シンガポール", "タイ", "香港", "フィリピン",
                 "マレーシア", "ベトナム", "インドネシア", "カンボジア", "UAE", "インド")

# ★cross-check DA (D1): 「実績」は非売上文脈 (採用実績/PJ実績) が多く過剰包含 → 除外
_SALES_RE = re.compile(r"売り?上げ?|売上|客数|客単価")
# 「日本」を国名として認識 (日本一/日本語/日本中/日本橋/日本人 等の複合語は除外)
_JAPAN_NATION_RE = re.compile(r"日本(?![一語中橋人式的海列])")
# 全社/グローバル明示 (★2026-07-20 海山: うみやまAI は日本デフォルト、全社は明示時のみ)。
# ★cross-check: 「全店」= 日本 scope の all-store 概念で全社ではない → 除外。「全体」も
# 「関東エリア全体」等で誤爆するため除外。プロンプトの全社トリガー (全部含む) と語彙を揃える。
_ALLCO_RE = re.compile(r"全社|全部|グローバル|世界|海外|連結|グループ全体|会社全体")
_SECTION_RE = re.compile(r"^## (\d{4}-\d{2}-\d{2})", re.MULTILINE)
_ROW_RE = re.compile(
    r"^\|\s*\d+\s*\|\s*([^|]+?)\s*\|\s*([\d,]+)\s*\|\s*([\d,]+)\s*\|", re.MULTILINE)
_COMMA_NUM_RE = re.compile(r"\d{1,3}(?:,\d{3})+")

MAX_DAYS = 14
MAX_CHARS = 7500


# ── 日付範囲の決定論解決 ─────────────────────────────────
def resolve_range(query: str, today: date) -> tuple[list[date], str] | None:
    """query 中の相対/絶対日付表現を日付リストへ。解決不能なら None。
    週は月曜はじまり (「先週」= 直前の月〜日。捏造事故の一因 = bot が月曜を落とした)。"""
    monday = today - timedelta(days=today.weekday())
    if "先々週" in query:
        s = monday - timedelta(days=14)
        return [s + timedelta(days=i) for i in range(7)], f"先々週 ({s} 月〜{s + timedelta(days=6)} 日)"
    if "先週" in query:
        s = monday - timedelta(days=7)
        return [s + timedelta(days=i) for i in range(7)], f"先週 ({s} 月〜{s + timedelta(days=6)} 日)"
    if "今週" in query:
        n = (today - monday).days + 1
        return [monday + timedelta(days=i) for i in range(n)], f"今週 ({monday} 月〜{today}、進行中)"
    if "一昨日" in query:
        d = today - timedelta(days=2)
        return [d], f"一昨日 ({d})"
    if "昨日" in query:
        d = today - timedelta(days=1)
        return [d], f"昨日 ({d})"
    m = re.search(r"(?:過去|直近|この)\s*(\d{1,2})\s*日", query)
    if m:
        n = min(int(m.group(1)), MAX_DAYS)
        s = today - timedelta(days=n)
        return [s + timedelta(days=i) for i in range(n)], f"直近{n}日 ({s + timedelta(days=1)}〜{today - timedelta(days=1)})"
    # M月D日 (〜M月D日) / M/D(〜M/D)
    dm = re.findall(r"(\d{1,2})\s*[月/]\s*(\d{1,2})\s*日?", query)
    if dm:
        def _mk(mo: int, dy: int) -> date | None:
            try:
                d = date(today.year, mo, dy)
            except ValueError:
                return None
            # 未来日付は前年扱い (「12月25日の売上」を 1 月に聞くケース)
            return d if d <= today + timedelta(days=1) else date(today.year - 1, mo, dy)
        d1 = _mk(int(dm[0][0]), int(dm[0][1]))
        if d1 is None:
            return None
        if len(dm) >= 2:
            d2 = _mk(int(dm[1][0]), int(dm[1][1]))
            if d2 and d2 >= d1 and (d2 - d1).days < MAX_DAYS:
                n = (d2 - d1).days + 1
                return [d1 + timedelta(days=i) for i in range(n)], f"{d1}〜{d2}"
        return [d1], f"{d1}"
    if "今日" in query or "本日" in query:
        return [today], f"本日 ({today}、進行中・部分値)"
    return None


# ── 次元とエンティティの検出 ─────────────────────────────
def detect_dimension(query: str) -> tuple[str, list[str]] | None:
    """(次元 key, 行名フィルタ) を返す。売上系ワード無し or 次元不明なら None。
    v1 は breakdown 4 次元のみ (全社 totaldaily は表形式が異なるため対象外 = 誤爆より縮退)。

    ★2026-07-20 海山: うみやまAI は日本事業がデフォルト。scope 明示無しの売上 query は
    日本 (nation) を default にする (= 全社を勝手に出さない)。「全社/グローバル/海外」明示時は
    None を返し全社 totaldaily 経路に委ねる。"""
    if not _SALES_RE.search(query):
        return None
    if _ALLCO_RE.search(query):  # 全社明示 → 全社 totaldaily 経路 (ここでは注入しない)
        return None
    if "業態" in query:
        return "type", []
    if "リーグ" in query:
        return "league", []
    areas = [t for t in AREA_TOKENS if t in query]
    if areas or "エリア" in query:
        return "area", areas
    # 「日本の売上」型を nation として拾う (日本一/日本語/日本橋 等の非国名は除外)
    japan_nation = bool(_JAPAN_NATION_RE.search(query))
    other_nations = [t for t in NATION_TOKENS if t != "日本" and t in query]
    if "国別" in query or japan_nation or other_nations:
        return "nation", [t for t in NATION_TOKENS if t in query]
    # ★scope 明示無し → 日本 default (海山: 日本以外は聞かれた時のみ)
    return "nation", ["日本"]


# ── セクション抽出 + 決定論集計 ──────────────────────────
def _parse_sections(md: str) -> dict[str, str]:
    """'## YYYY-MM-DD …' 単位で分割 (次の ## まで)。"""
    out: dict[str, str] = {}
    matches = list(_SECTION_RE.finditer(md))
    for i, m in enumerate(matches):
        end = matches[i + 1].start() if i + 1 < len(matches) else len(md)
        out[m.group(1)] = md[m.start():end].rstrip()
    return out


def _filter_section(section: str, tokens: list[str]) -> str:
    """行名フィルタ (例: 関東 → 関東A/関東B/関東 の行のみ)。ヘッダ行は常に残す。"""
    if not tokens:
        return section
    kept = []
    for ln in section.splitlines():
        if not ln.startswith("|"):
            kept.append(ln)
            continue
        if ln.startswith("| #") or ln.startswith("|--") or ln.startswith("|---"):
            kept.append(ln)
            continue
        if any(t in ln for t in tokens):
            kept.append(ln)
    return "\n".join(kept)


def _totals(sections: list[str], tokens: list[str], partial: bool = False) -> str:
    """フィルタ後セクション群から entity 別 + 合算の決定論集計を作る。
    「関東エリア」型の恒常 0 合算行は集計から除外 (誤読の元)。
    ★cross-check DA (D5b): 部分窓 (欠落日あり) の合計を「期間の合計」とラベルすると
    無音の過少報告に化ける → partial 時はラベルで明示。"""
    per: dict[str, list[int]] = {}
    for sec in sections:
        for name, cust, sales in _ROW_RE.findall(sec):
            name = name.strip()
            if tokens and not any(t in name for t in tokens):
                continue
            c, s = int(cust.replace(",", "")), int(sales.replace(",", ""))
            if c == 0 and s == 0:
                continue  # 恒常 0 の合算行 (関東エリア等) は集計に混ぜない
            acc = per.setdefault(name, [0, 0])
            acc[0] += c
            acc[1] += s
    if not per:
        return ""
    if partial:
        lines = [f"◆ 決定論集計 (※データが存在する {len(sections)} 日分のみの部分合計 — "
                 "期間全体の合計ではない):"]
    else:
        lines = ["◆ 決定論集計 (上記期間の合計、再計算不要):"]
    tc = ts = 0
    for name, (c, s) in sorted(per.items(), key=lambda kv: -kv[1][1]):
        u = round(s / c) if c else 0
        lines.append(f"- {name}: 客数 {c:,} / 売上 {s:,}円 / 客単価 {u:,}円")
        tc += c
        ts += s
    if len(per) > 1:
        u = round(ts / tc) if tc else 0
        lines.append(f"- 合計 ({'+'.join(sorted(per))}): 客数 {tc:,} / 売上 {ts:,}円 / 客単価 {u:,}円")
    return "\n".join(lines)


def build_context(query: str, *, today: date | None = None,
                  knowledge_dir: Path | None = None) -> str | None:
    """注入ブロックを組み立てる。トリガー外・データ無しは None (通常フロー)。"""
    dim = detect_dimension(query or "")
    if not dim:
        return None
    rng = resolve_range(query, today or date.today())
    if not rng:
        return None
    dim_key, tokens = dim
    dates, label = rng
    # ★cross-check: 当日のみ (今日/本日) は集計途中 + nationdaily 未生成のことがあり、live の
    # daily-sales.md に委ねる (honest「データ無し」block で live 回答を抑制しないため None)。
    if not any(d < (today or date.today()) for d in dates):
        return None
    # ★2026-07-20 海山: scope 明示無しは日本 default。その旨を label に明示 (bot が scope 透明化)。
    scope_defaulted = (dim_key == "nation" and tokens == ["日本"]
                       and not _JAPAN_NATION_RE.search(query or "") and "国別" not in (query or ""))
    if scope_defaulted:
        label = f"{label}・日本 (scope 指定なし = 日本 default、全社/海外は聞かれた時のみ)"
    if knowledge_dir is None:
        return None
    path = Path(knowledge_dir) / HISTORY_FILES[dim_key]
    try:
        md = path.read_text(encoding="utf-8")
    except OSError:
        return None
    sections = _parse_sections(md)
    hit, missing = [], []
    for d in dates:
        key = d.isoformat()
        if key in sections:
            hit.append(_filter_section(sections[key], tokens))
        else:
            missing.append(key)
    if not hit:
        # ★cross-check DA (D5a): 全欠落で None を返すと通常の vector 経路に落ち、
        # 「部分 hit → 捏造」の事故クラスが復活する。売上×日付×次元が揃った質問である
        # ことは確定しているので、「無い」と言わせる明示ブロックを返す。
        return (f"【参照データ: 日次売上履歴 — {label}】\n"
                f"⚠ この期間 ({', '.join(missing)}) の日次データは手元の履歴に存在しない "
                f"(履歴の保持期間外か未取得)。推測で数字を作らず、"
                f"「その期間の日次データは手元に無い」と正直に伝える。")
    body = "\n\n".join(hit)
    if len(body) > MAX_CHARS:
        body = body[:MAX_CHARS] + "\n(…以降のセクションは省略。集計は下の決定論値が正)"
    parts = [
        f"【参照データ: 日次売上履歴 (確定値) — {label}】",
        "※合算名の行 (例:「関東エリア」) は常に 0 (システム上の空行)。実態は下位区分の合計 "
        "(例: 関東 = 関東A + 関東B)。",
        body,
    ]
    totals = _totals(hit, tokens, partial=bool(missing))
    if totals:
        parts.append(totals)
    if missing:
        parts.append(f"⚠ この範囲でデータが存在しない日: {', '.join(missing)} — "
                     "この日の数字は無い。推測で埋めずに「データが無い」と伝え、"
                     "◆集計は部分合計である旨を必ず添える (期間全体の合計として言わない)。")
        parts.append(
            "【指示】売上・客数・客単価は必ずこのブロック内の数値をそのまま使う。ブロックに"
            "無い日付・数値を作らない。欠落日があるため期間全体の合計は算出できない。")
    else:
        parts.append(
            "【指示】売上・客数・客単価は必ずこのブロック内の数値をそのまま使う。ブロックに"
            "無い日付・数値を作らない。期間合計は◆決定論集計の値を使い、自分で再計算しない。")
    return "\n".join(parts)


# ── 応答後の数値ガード (tenpo numeric_guard と同型) ───────
def sales_numeric_guard(reply: str, block: str) -> str:
    """応答中の**カンマ区切り精密数値**が注入ブロックに無い (= LLM が数値を作った) 場合、
    確定値の決定論集計を追記。false positive でも追記されるのは正値なので安全側。
    ★cross-check DA (D2): 億/万 の丸め表現は検知対象にしない — 履歴表はプレーン円のみで
    億/万 token を含まないため、「約1.9億」等の自然な丸めで毎回発火して全売上応答が
    脚注だらけになる (高頻度経路では tenpo の「安全側」論が冗長化として跳ね返る)。
    捏造検知の本丸は「実在しない精密値」なのでカンマ数値に限定する。"""
    if not reply or not block:
        return reply
    block_tokens = set(_COMMA_NUM_RE.findall(block))
    # 億/万 が付いた数は丸め表現 (「約2,000万」等) として走査対象から除去
    reply_scan = re.sub(r"[\d,.]+(?:億|万)", "", reply)
    bad = [t for t in _COMMA_NUM_RE.findall(reply_scan) if t not in block_tokens]
    if not bad:
        return reply
    agg = [ln for ln in block.splitlines() if ln.startswith(("◆", "- ")) and "客数" in ln]
    if not agg:
        return reply
    return reply + "\n\n※数値は日次履歴の確定値が正:\n" + "\n".join(agg)
