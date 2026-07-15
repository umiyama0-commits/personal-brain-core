"""
brain_wiki_helpers/store_keyword.py — クエリ中の店舗名検出

★2026-05-22 Phase 1c 切り出し:
brain_wiki.BrainWiki._detect_store_keyword を pure function 化。
self に依存していなかったので、そのまま module-level function 化。
"""
from __future__ import annotations

import re
from typing import Optional


# 汎用 suffix の blocklist (誤マッチの主犯)
SUFFIX_BLOCKLIST: set[str] = {
    "モール", "ストア", "シティ", "プラザ", "センター",
    "タウン", "ガーデン", "ステーション", "シネマ", "アウトレット",
    "ショップ", "マーケット", "パーク", "本店", "支店",
}

# 汎用 prefix の blocklist (これだけだと候補が複数になり過ぎる)
_PREFIX_BLOCKLIST: set[str] = {
    "イオン", "イオンモール", "イオンタウン", "イオンスタイル",
    "ららぽーと", "サンエー", "ゆめタウン", "ヴェルサ",
    "セブン", "Central", "SM City", "WonderGOO",
    "Robinson", "Aeon Mall", "新光三越", "佳瑪",
}

# 地名 (都市・県・国) は店舗特定に使えない。たまたま 1 店舗で unique でも誤検出するので blocklist
# ★過去事故: 「東京の売上」 → 「東京ドームシティ ラクーア」 と誤検出 (東京で始まる店が 1 店だけのため)
_LOCATION_BLOCKLIST: set[str] = {
    # 国
    "日本", "シンガポール", "台湾", "香港", "タイ", "マレーシア",
    "ベトナム", "フィリピン", "インド", "インドネシア", "UAE", "アメリカ",
    "カンボジア", "オーストラリア", "オランダ", "ロシア", "ラオス",
    # 主要都市
    "東京", "大阪", "名古屋", "福岡", "札幌", "神戸", "横浜", "京都",
    "仙台", "広島", "岡山", "熊本", "鹿児島", "千葉", "埼玉", "新潟",
    "金沢", "静岡", "浜松", "高松", "大分", "宮崎", "佐賀", "長崎",
    # 都道府県・エリア
    "沖縄", "関東", "関西", "九州", "東北", "中部", "北海道", "東日本", "西日本",
    "渋谷", "新宿", "原宿", "六本木",  # ambiguous 多店舗エリア
}

PREFIX_BLOCKLIST: set[str] = _PREFIX_BLOCKLIST | _LOCATION_BLOCKLIST

# クエリ tokenize 用 separator
# ★重要: 「と」は「ららぽーと」の「と」と衝突するので separator に入れない
QUERY_SEPARATOR = re.compile(
    r"[\s　・、，。,.！？!?]+|"
    r"(?:の|を|に|で|は|が|から|まで|より|や|店舗|店|"
    r"最近|過去|直近|今日|本日|昨日|一昨日|今週|先週|今月|先月|今年|去年|"
    r"売上|客数|客単価|単価|営業|業績|数字|実績|状況|成績|推移|傾向|"
    r"日|週|週間|ヶ月|ヵ月|か月|年|前|間|分|だけ|くらい|程度|"
    r"円|JPY|万円|億円|%|％)"
)


def detect_store_keyword(
    query: str, stores_content: str, daily_stores_content: str = ""
) -> Optional[str]:
    """クエリ中に「店舗名」が含まれているかを店舗一覧と双方向マッチングして検出する。

    ★2026-04-30 v1: 「八幡東店」のような prefix 省略表現に対応 (suffix match)
    ★2026-05-01 v2:
      - daily-stores.md (今日) の店舗名も検索対象に追加
        (= 新店で history が無い店舗も検出できる: イオンモール伊達 / アスティ静岡 等)
      - 接尾辞 match の最小長さを 3 → 4 に強化 (汎用語 "モール"/"センター"/"プラザ"/"店" 等の誤マッチ防止)
      - 全店共通の汎用 suffix を blocklist で除外
    ★2026-05-15 v3 (重大バグ修正):
      - 「武蔵小山」「池袋」「川崎」のような店名 prefix での検出に対応 (prefix match)
      - クエリの tokenize (「の」「店」「最近」「売上」等の助詞・修飾語で分割)
      - 「武蔵小山店の売上」「武蔵小山の最近」も検出できるように
      - 過去事故: 「武蔵小山の最近の売上は?」が None → bot がデータあるのに「ない」と返答

    マッチ戦略 (優先順):
    1. 完全一致: 店名がクエリ内に丸ごと含まれる (highest priority、+200 ボーナス)
    2. クエリ tokenize → 4 文字以上の token で店名 prefix match
       - Unique (= ただ一つの店舗に prefix match) のみ採用
       - +100 ボーナス (suffix match より優先、完全一致より下)
    3. 接尾辞一致: 店名末尾 N 文字 (4 ≤ N、blocklist 除外) がクエリ内に含まれる
    4. 同点時: より長い match を優先
    """
    store_names: set[str] = set()
    # history-stores.md (過去): `| # | code | name | qty | sales | currency |`
    for m in re.finditer(
        r"\|\s*\d+\s*\|\s*\d+\s*\|\s*([^\|]+?)\s*\|\s*[\d,]+\s*\|",
        stores_content,
    ):
        name = m.group(1).strip()
        if 2 <= len(name) <= 30 and name not in {"店舗名"}:
            store_names.add(name)
    # daily-stores.md (今日): `| # | code | name | customer | (JPY)X |`
    if daily_stores_content:
        for m in re.finditer(
            r"\|\s*\d+\s*\|\s*\d+\s*\|\s*([^\|]+?)\s*\|\s*\d+\s*\|\s*\(",
            daily_stores_content,
        ):
            name = m.group(1).strip()
            if 2 <= len(name) <= 30 and name not in {"店舗名"}:
                store_names.add(name)
    if not store_names:
        return None

    best_score = 0
    best_name: Optional[str] = None

    # 1) 完全一致 (highest priority、店名そのままクエリ内に含まれる)
    for name in sorted(store_names):
        if name in query:
            score = len(name) + 200
            if score > best_score:
                best_score = score
                best_name = name

    # 2) クエリ tokenize → prefix match (★2026-05-15 追加)
    #    クエリから「の」「店」「最近」「売上」等の助詞・修飾語を削除して残った断片を取得
    #    例: 「武蔵小山の最近の売上は?」 → tokens=["武蔵小山", "最近", "売上"]
    #        → "武蔵小山" prefix で store_names を絞り込み → unique なら採用
    raw_tokens = QUERY_SEPARATOR.split(query)
    # 長さ 3 以上の token を採用 (「池袋」「錦糸町」等 2-3 字の地名もキャッチ)
    tokens = [t.strip() for t in raw_tokens if t and 3 <= len(t.strip()) <= 15]

    # 各 token で prefix match を試す (unique 採用)
    for token in tokens:
        if token in PREFIX_BLOCKLIST:
            continue
        matches = [name for name in store_names if name.startswith(token)]
        if len(matches) == 1:
            # Unique match → 採用
            name = matches[0]
            score = len(token) + 100
            if score > best_score:
                best_score = score
                best_name = name
        # 複数候補は silent skip (LLM 側で聞き返してもらう)

    # 2.5) 短い prefix (2 字) でも unique なら採用 (「池袋」「川崎」「上野」等)
    #      store_names から 2 字 prefix の逆引きを構築 → クエリに含まれるか + unique チェック
    # ★過去事故: 「池袋の売上は?」「川崎ダイスは?」が None になっていた
    if best_score == 0 or best_name is None:  # まだ何も match してない場合
        short_prefix_map: dict[str, list[str]] = {}
        for name in store_names:
            if len(name) >= 2:
                short_prefix_map.setdefault(name[:2], []).append(name)
            if len(name) >= 3:
                short_prefix_map.setdefault(name[:3], []).append(name)

        # 各短 prefix がクエリに含まれてて、unique なら採用
        for prefix, names in short_prefix_map.items():
            if len(names) != 1:
                continue  # 複数候補は曖昧
            if prefix in PREFIX_BLOCKLIST:
                continue
            if prefix in query:
                name = names[0]
                # 短 prefix なのでスコアは低め (+50)
                score = len(prefix) + 50
                if score > best_score:
                    best_score = score
                    best_name = name

    # 3) 接尾辞一致 (3 文字以上、blocklist 外、既存ロジック緩和)
    # ★2026-05-15: 4→3 に緩和 (「八幡東」「川崎」suffix もキャッチ)
    for name in sorted(store_names):
        if name in query:
            continue  # 完全一致は既に処理済
        for suffix_len in range(min(len(name), 15), 2, -1):
            suffix = name[-suffix_len:]
            if suffix in SUFFIX_BLOCKLIST:
                continue
            if suffix in query:
                if suffix_len > best_score:
                    best_score = suffix_len
                    best_name = name
                break

    return best_name


# ───────────────────────────────────────────────────────────────────
# ★2026-06-08 海山指摘 (大須 follow-up「直近3ヶ月は?」で店舗が落ちる) の
#   context-aware 店舗解決。cross-check 3種 (Reviewer/DA/Fact-checker) 反映:
#   - retrieval は single-turn で会話文脈の店舗を解決しない (= 根本原因)
#   - 素朴に history 走査すると「最長名 match」で assistant ランキングから誤店舗を
#     拾い、head 移動 + prompt で増幅され「自信満々の誤答」(= 無いより悪い)
#   → 保守的 gate: user ターンのみ / window=1 / ターン内複数店=None / reset語=None。
#     「確信が無ければ埋めない」(誤注入 >> データ無し のコスト非対称) を優先。
# ───────────────────────────────────────────────────────────────────

# 「全社/全店」系 = 店舗継続を切る reset marker (これが有れば文脈店舗を引かない)
RESET_MARKERS: tuple[str, ...] = (
    "全社", "全店", "全部", "全体", "世界", "グローバル", "会社全体",
    "グループ全体", "グループ", "全社合計", "国内全", "海外全",
)


def has_reset_marker(text: str) -> bool:
    """「全社」等、店舗スコープを全体に戻す語を含むか。"""
    return any(m in (text or "") for m in RESET_MARKERS)


def _collect_store_names(stores_content: str, daily_stores_content: str = "") -> set[str]:
    """detect_store_keyword と同一ロジックで店舗名 set を構築 (曖昧判定で再利用)。"""
    names: set[str] = set()
    for m in re.finditer(
        r"\|\s*\d+\s*\|\s*\d+\s*\|\s*([^\|]+?)\s*\|\s*[\d,]+\s*\|",
        stores_content,
    ):
        name = m.group(1).strip()
        if 2 <= len(name) <= 30 and name not in {"店舗名"}:
            names.add(name)
    if daily_stores_content:
        for m in re.finditer(
            r"\|\s*\d+\s*\|\s*\d+\s*\|\s*([^\|]+?)\s*\|\s*\d+\s*\|\s*\(",
            daily_stores_content,
        ):
            name = m.group(1).strip()
            if 2 <= len(name) <= 30 and name not in {"店舗名"}:
                names.add(name)
    return names


def _distinct_store_matches(text: str, store_names: set[str]) -> set[str]:
    """text 中に「明示的に」登場する店舗名の集合 (曖昧 = 複数店 判定用)。

    完全一致 + 4 文字以上の prefix/suffix 一致 (blocklist 除外) を「明示」とみなす。
    弱い 2-3 字 prefix は spurious が多いので曖昧判定には使わない (誤って None 連発を防ぐ)。
    """
    found: set[str] = set()
    for name in store_names:
        if name in text:
            found.add(name)
            continue
        # suffix 一致 (4 字以上、blocklist 外) — 「名古屋大須」を「大須」で
        for slen in range(min(len(name), 15), 3, -1):
            suf = name[-slen:]
            if suf in SUFFIX_BLOCKLIST:
                continue
            if suf in text:
                found.add(name)
                break
    return found


def resolve_store_from_history(
    history: list | None,
    stores_content: str,
    daily_stores_content: str = "",
    window: int = 1,
) -> Optional[str]:
    """直近の **user** ターン (window 件) から単一店舗を解決する。

    呼び出し側で「現 query に店舗なし & 現 query が期間に解決 (year_months 非空) &
    現 query に reset 語なし」を gate 済みである前提 (= period follow-up の継続)。

    保守的ルール (cross-check DA 反映、誤注入回避優先):
    - **user ターンのみ** 走査 (assistant のランキング文から誤店舗を拾わない)
    - **window=1** (直前 user ターンのみ) default — 古い店舗の引きずり防止
    - 走査ターンに **reset 語** があれば None (直前が全社文脈なら継続しない)
    - 走査ターンに **複数の明示店舗** があれば None (「大須と渋谷どっち」= 曖昧)
    返り値: 解決した店舗名 or None。
    """
    if not history:
        return None
    store_names = _collect_store_names(stores_content, daily_stores_content)
    if not store_names:
        return None
    user_texts = [h.get("content", "") for h in history
                  if isinstance(h, dict) and h.get("role") == "user"]
    for text in reversed(user_texts[-window:]):
        if not text:
            continue
        if has_reset_marker(text):
            return None  # 直前が全社 → 店舗継続しない
        present = _distinct_store_matches(text, store_names)
        if len(present) == 1:
            # 念のため detect_store_keyword (スコア最良) と整合する 1 店を返す
            kw = detect_store_keyword(text, stores_content, daily_stores_content)
            return kw if kw in present else next(iter(present))
        if len(present) >= 2:
            return None  # 曖昧 (複数店明示) → 解決しない
    return None
