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
    # 語境界判定用 (2 字の「池袋」等も残す。部分文字列 latch を防ぐ土台)
    boundary_tokens = [t.strip() for t in raw_tokens if t and t.strip()]

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

        # 各短 prefix が **token の先頭** と一致してて、unique なら採用
        # ★2026-08-06 事故: 判定が `prefix in query` (語境界を見ない素の部分文字列) だったため、
        #   「イ|オン|浦安」の中の『オン』が 738 店中ただ 1 つ『オン』で始まる
        #   「オンデーズオンラインショップ」に unique match し、**別店舗の売上が注入**されていた。
        #   実測: 「イオンモール○○」を「イオン○○」と略した 291 店中 67 店 (23%) が
        #   全滅 (正解 0%)、実ログでも店舗絡み売上質問 118 件中 82 件 (69%) が誤発火。
        #   token 境界を要求すれば「池袋」「川崎ダイス」等の本来の用途は保たれる
        #   (「イオン浦安」.startswith("オン") は False = latch しない)。
        for prefix, names in short_prefix_map.items():
            if len(names) != 1:
                continue  # 複数候補は曖昧
            if prefix in PREFIX_BLOCKLIST:
                continue
            # token を prefix がほぼ覆っている時だけ採用する。
            # ★2026-08-06: 「立川立飛」の頭 2 字『立川』が別店「立川高島屋S.C.」に
            #   unique match し、正解「ららぽーと立川立飛」を押し退けていた。
            #   token の残りが 2 字以上あるなら、それは別の店を指している合図。
            # ★2026-08-06 追補: slack (1 字) を許すのは日本語 prefix だけ。
            #   英字は語が長く「Mal」が「Mall」を捕まえるため、`AEON Mall の売上は?`
            #   が常にインドネシアの 1 店 (Mal SKA Pekanbaru) に化けていた (実測)。
            _slack = 0 if prefix.isascii() else 1
            if any(t.startswith(prefix) and len(t) - len(prefix) <= _slack
                   for t in boundary_tokens):
                name = names[0]
                # 短 prefix なのでスコアは低め (+50)
                score = len(prefix) + 50
                if score > best_score:
                    best_score = score
                    best_name = name

    # 2.7) 表記ゆれ吸収 (★2026-08-06)。社員は正式名で呼ばない —
    #      「イオン浦安」(正式: イオン新浦安)、「イオン津田沼」(正式: イオンモール津田沼North)
    #      のように **文字が抜けた** 形で聞く。token の文字が順序を保って店名に現れる
    #      (= 部分列) 店を探し、**一意な時だけ** 採用する。
    #      3) の接尾辞一致より先に置く: 接尾辞は語境界を見ないため
    #      「イオ|ン出雲」の『ン出雲』が「ゆめタウン出雲」に当たる等、
    #      別チェーンへ流れる (実測 72 件中 3 件)。一意な部分列の方が強い証拠。
    # ★2026-08-06 追補: 発火条件が `best_name is None` だと、2.5) の弱い 2-3 字 prefix
    #   (+50) が先に埋めた時点で正解の部分列ヒットが捨てられる。
    #   実測 (740 店 × 1 字脱落 8,016 query): この順序バグだけで
    #   「修正前は正解 → 修正後は別店」の退行が 47 件出ていた (うち 45 件は部分列が
    #   正解を持っていた)。一意な部分列は短 prefix より強い証拠なので +80 で競合させる。
    ambiguous = False
    if best_score < 100:  # 完全一致 (+200) / 長 token prefix (+100) 以外
        _seq, ambiguous = _resolve_by_subsequence(boundary_tokens, store_names)
        if _seq is not None and 80 > best_score - 100:
            _seq_score = len(_seq) + 80
            if _seq_score > best_score:
                best_score = _seq_score
                best_name = _seq

    # 3) 接尾辞一致 (3 文字以上、blocklist 外、既存ロジック緩和)
    # ★2026-05-15: 4→3 に緩和 (「八幡東」「川崎」suffix もキャッチ)
    # ★2026-08-06: 曖昧が判明している時は接尾辞で推測しない。
    #   「イオン郡山」は イオンモール郡山 / イオンタウン郡山 の2店に当たるが、
    #   接尾辞一致は語境界を見ないため『ン郡山』でタウン側に倒れていた。
    #   誤注入 >> データ無し のコスト非対称に従い、曖昧なら聞き返させる。
    if best_name is not None or ambiguous:
        return best_name
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


# 部分列マッチの最短 token 長。3 字以下だと候補が爆発し誤検出コストが利得を上回る
SUBSEQ_MIN_LEN = 4


def _is_subsequence(needle: str, haystack: str) -> bool:
    """needle の全文字が順序を保って haystack に現れるか。"""
    it = iter(haystack)
    return all(ch in it for ch in needle)


def _resolve_by_subsequence(tokens, store_names) -> tuple[Optional[str], bool]:
    """表記ゆれ (文字の脱落) を吸収する。戻り値は (店名 or None, 曖昧だったか)。

    「確信が無ければ埋めない」(誤注入 >> データ無し のコスト非対称) の原則は維持する —
    複数店に当たったら None + ambiguous=True を返し、以降の弱い推測も止めて聞き返させる。
    """
    ambiguous = False
    for token in sorted(tokens, key=len, reverse=True):
        if len(token) < SUBSEQ_MIN_LEN or token in PREFIX_BLOCKLIST:
            continue
        hits = [n for n in store_names if len(n) > len(token) and _is_subsequence(token, n)]
        if len(hits) == 1:
            return hits[0], False
        if len(hits) > 1:
            ambiguous = True
    return None, ambiguous


# ───────────────────────────────────────────────────────────────────
# ★2026-08-09 海山指示: 「吉祥寺は今後 吉祥寺店 と 吉祥寺マルイ店 が並ぶ。
#   新宿・銀座のように 2 文字で聞かれるケースもある」
#
#   従来は曖昧な地名を _LOCATION_BLOCKLIST で **丸ごと封鎖** していた
#   (新宿/渋谷/横浜/名古屋)。誤検出は防げるが、社員が最も使う呼び方に
#   一切答えられない = 「安全だが役に立たない」状態だった。
#   実測: 新宿 3店 (アルタ/マルイアネックス/東口)、吉祥寺 2店、渋谷 2店、横浜 3店。
#
#   **黙るのではなく候補を返す**。「新宿は3店ありますが、どれですか」と
#   聞き返せる方が、社員にとって明らかに有用。候補は店舗マスターから
#   決定論的に引くので捏造は起きない。
# ───────────────────────────────────────────────────────────────────

# 地名として扱わない token (店名に偶然含まれても候補にしない)
_NON_PLACE_TOKENS: frozenset = frozenset({
    "売上", "客数", "客単価", "実績", "予算", "全社", "全店", "本部", "会社",
    "今日", "本日", "昨日", "今週", "先週", "今月", "先月", "今年", "去年",
})
PLACE_MIN_LEN = 2       # 「新宿」「銀座」= 2 文字を拾う
PLACE_MAX_CANDIDATES = 6  # これを超える token は地名でなく汎用語とみなす


def resolve_store(
    query: str, stores_content: str, daily_stores_content: str = ""
) -> tuple[Optional[str], list[str]]:
    """店舗を解決する。戻り値 = (確定した店名 or None, 候補一覧)。

    候補が 1 つに絞れれば第 1 要素に入り、複数なら第 2 要素だけが埋まる
    (= 呼び手が「どれですか」と聞き返す)。どちらも空なら店舗質問ではない。

    detect_store_keyword (従来の厳格判定) を先に通し、それが None の時だけ
    地名の部分一致で候補を集める = 既存の精度は一切下げない。
    """
    store_names = _collect_store_names(stores_content, daily_stores_content)
    if not store_names:
        return None, []

    exact = detect_store_keyword(query, stores_content, daily_stores_content)
    if exact:
        # ★2026-08-09 海山指摘「吉祥寺は今後 吉祥寺店 と 吉祥寺マルイ店 が並ぶ」:
        #   店名そのものが別店の **前方部分** になっている時 (吉祥寺 ⊂ 吉祥寺マルイ)、
        #   完全一致で 1 店に確定すると **2 店あるのに聞き返さず片方を答える**。
        #   確定名を含むより長い店名があれば、候補として一緒に返す。
        siblings = sorted(n for n in store_names if n != exact and exact in n)
        if siblings:
            return None, sorted([exact] + siblings)
        return exact, [exact]

    tokens = [t.strip() for t in QUERY_SEPARATOR.split(query) if t and t.strip()]
    best: list[str] = []
    for token in sorted(tokens, key=len, reverse=True):
        if len(token) < PLACE_MIN_LEN or token in _NON_PLACE_TOKENS:
            continue
        hits = sorted(n for n in store_names if token in n)
        if not hits:
            # 「イオン郡山」のように施設 prefix 付きで呼ばれた時は、
            # 部分列 (文字が順序を保って現れる) で候補を集める。
            # 実測: イオンモール郡山 / イオンタウン郡山 の 2 店に当たるので
            # 「どちらですか」と聞き返せる (従来は候補ゼロで黙っていた)。
            hits = sorted(n for n in store_names
                          if len(n) > len(token) and _is_subsequence(token, n))
        # 候補が多すぎる = 地名でなく汎用語 (「モール」等) を掴んでいる
        if hits and len(hits) <= PLACE_MAX_CANDIDATES:
            best = hits
            break
    if len(best) == 1:
        return best[0], best
    return None, best


def resolve_reply_against_candidates(
    reply: str, candidates: list[str]
) -> Optional[str]:
    """聞き返しへの返信を **候補集合の中だけ** で解決する (★2026-08-10)。

    事故 (再ローンチ前の実測): bot が「新宿は 3 店: アルタ / マルイアネックス / 東口」と
    聞き返した後、社員が「アルタ」と返すと、全店マスターに対する検出が
    **アル・プラザ鹿島** (「アル」2 字 prefix latch) に化けていた。「マルイ」も
    マルイファミリー溝口 に化ける。= 聞き返しループが閉じず、別店の数字を出す寸前だった。

    候補は直前ターンで確定済みの集合なので、その中だけで照合すれば
    弱い全店マッチより常に強い証拠になる。一意に絞れない時は None (もう一度聞き返す)。
    """
    if not reply or not candidates:
        return None
    if has_reset_marker(reply):
        return None  # 「全社で」等 = 店舗の文脈を抜けた
    # 1) 候補名がそのまま返信に含まれる (「新宿アルタで」)
    full = [c for c in candidates if c in reply]
    if len(full) == 1:
        return full[0]
    if len(full) > 1:
        # 「吉祥寺マルイで」は 吉祥寺 と 吉祥寺マルイ の両方に完全一致する。
        # 最長の候補が他の一致候補を全て内包するなら、それが指名 (より具体的な方が勝つ)。
        longest = max(full, key=len)
        if all(c in longest for c in full):
            return longest
        return None  # 別々の店を両方 named = まだ曖昧
    # 2) 返信 token が候補名の一部 (「アルタ」⊂「新宿アルタ」)、だめなら部分列
    toks = [t.strip() for t in QUERY_SEPARATOR.split(reply)
            if t and len(t.strip()) >= 2]
    for t in sorted(toks, key=len, reverse=True):
        hits = [c for c in candidates if t in c]
        if not hits:
            hits = [c for c in candidates if _is_subsequence(t, c)]
        if len(hits) == 1:
            return hits[0]
        if len(hits) > 1:
            return None  # この token では絞れない = 聞き直す
    return None


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
