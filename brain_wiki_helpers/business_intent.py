"""brain_wiki_helpers/business_intent.py — 業務データ質問の決定論判定
(★2026-07-20 §1.15 cross-check: 売上の数字は必ず canonical+guard を通すため、agent の
未ガード経路に落とさず clone 回答エンジンへ pre-route する判定を pure function で分離)。

main.py (fastapi 依存) から切り出して単体テスト可能に (§1.12b: ロジックは helper へ)。
high-recall 寄り: 取りこぼすと未ガードで答えてしまうため業務データっぽいものは広めに拾う。
誤って personal を業務判定しても clone は public scope の答えを返すだけ (実害小)。
"""
from __future__ import annotations

import json
import re

_BIZ_SALES_RE = re.compile(
    r"売上|売り上げ|客数|客単価|予算(?:比|達成)?|前年(?:比|同)?|前月比|昨対|昨年対比|"
    r"既存店|全店|YoY|yoy|ＹｏＹ|"
    r"ランキング|順位|決算|粗利|日販|買上|来店|達成率|実績|SPH|UPT|セット率"
)
_BIZ_REGULATION_RE = re.compile(
    r"就業規則|公休|有給|産休|育休|介護休|副業|兼業|賞与|手当|給与|退職金|"
    r"社内規程|規程|人事制度|評価制度|勤怠|フレックス|時短"
)

# フォローアップ (直前が売上会話なら短い継続語も業務照会扱い)。単独では業務と断定しないが、
# 売上文脈の継続として拾う語 (国/エリア/次元/対比の切り口)。
_BIZ_FOLLOWUP_RE = re.compile(
    r"日本|台湾|シンガポール|タイ|香港|フィリピン|マレーシア|カンボジア|"
    r"関東|関西|九州|中部|東海|北海道|東北|沖縄|中国|四国|エリア|地域|"
    r"国別|業態|リーグ|店|前年|昨年|前月|同日|曜日|既存|全店|内訳|別で?$|"
    # ★2026-07-22 cross-check DA: 期間だけの継続 (「先週は?」「昨日は?」) が拾えず、
    #   設計説明と実装が乖離していた (宣伝していた型が動かない)。期間語を追加。
    r"先週|先々週|今週|今日|昨日|一昨日|今月|先月"
)


# ★cross-check DA: 国名/エリア語を含む非売上の雑談 (「台湾行きたい」「日本の天気」) を除外。
# これらが混じると売上会話の直後に誤って売上データ回答が返る intent 不一致。
_NOT_BUSINESS_RE = re.compile(
    r"天気|行きたい|旅行|観光|出張|ビザ|為替|レート|好き|嫌い|食べ|グルメ|美味|"
    r"元気|疲れ|眠|楽し|面白|映画|ニュース|地震|台風|コロナ|休み欲し"
)


_DATE_PHRASE_RE = re.compile(
    r"一昨日|昨日|先々週|先週|今週|本日|今日|"
    r"(?:過去|直近|この)\s*\d{1,2}\s*日|\d{1,2}\s*[月/]\s*\d{1,2}\s*日?")


def extract_date_phrase(message: str) -> str:
    """message 中の日付表現だけを抽出 (「先週の関東の売上」→「先週」)。無ければ ''。
    ★cross-check DA: follow-up 併合で前クエリの次元 (エリア/業態) を引き継がず日付文脈のみ渡す
    = 次元切替 follow-up (業態→国別) が前次元にシャドウされる事故を防ぐ。"""
    hits = _DATE_PHRASE_RE.findall(message or "")
    return " ".join(hits)


def is_business_followup(message: str) -> bool:
    """売上会話の継続に見える短い follow-up か (「日本の」「エリア別で」「既存店は?」等)。
    呼び手が『直前が売上応答』を確認した上で使う (単独判定には使わない)。"""
    m = (message or "").strip()
    if not m or len(m) > 25:  # 長文は独立質問として通常フロー
        return False
    if _NOT_BUSINESS_RE.search(m):  # 非売上の雑談は除外
        return False
    return bool(_BIZ_FOLLOWUP_RE.search(m))


def is_business_data_query(message: str, facility_detector=None) -> bool:
    """売上/客数等の業務データ・社内規程・施設商圏の照会か (= clone 回答エンジンへ pre-route)。

    facility_detector: 施設名/商圏を検出する callable (str -> truthy)。省略時は施設判定なし
    (main.py から lookup_service.clone_context を渡す。テストでは fake か省略)。
    """
    m = (message or "").strip()
    if not m:
        return False
    if _BIZ_SALES_RE.search(m) or _BIZ_REGULATION_RE.search(m):
        return True
    if facility_detector is not None:
        try:
            if facility_detector(m):
                return True
        except Exception:
            pass
    return False


# ─── ①follow-up 併合 / ②スロット解釈 (★2026-07-22 海山「社員クローンも少し agentic に」) ───
# §1.15 DA 生存形: LLM に道具を持たせず「文脈を運ぶ + 解釈だけ手伝わせ、数値は最後まで決定論」。
# 常時 tool-loop 案は ADR 2026-07-20 の桁事故理由①② + ガード closure 不発で棄却済み。


# 併合で前クエリから引き継ぐメトリック語 (injector の _SALES_RE / yoy トリガーを発火させる語)。
# ★次元語 (エリア/業態/国名) は絶対に引き継がない = 次元シャドウ防止 (cross-check DA)。
_METRIC_CARRY_RE = re.compile(r"既存店前年比|昨年対比|前年比|昨対|売り?上げ?|売上|客単価|客数")
# 日付引き継ぎを止める「別期間」語 (「今月の達成率は?」に前クエリの「昨日」を混ぜない)。
_OTHER_PERIOD_RE = re.compile(r"今月|先月|来月|今年|昨年|去年|来年|\d{1,2}\s*月")
# ★cross-check Reviewer: 直前 turn が system 生成の placeholder (資料アップロード等) の時は
#   併合しない。ファイル名に日付+売上語が入ると「資料についての質問」に wiki 数値が注入され、
#   資料ベースの回答を上書きする (実例: `[資料アップロード: 6/15売上速報_日本.pdf]`)。
_PLACEHOLDER_PREV_RE = re.compile(r"^\s*[\[【](?:資料|ファイル|画像|button|ボタン|システム)")


def is_fresh_followup(prev_ts_iso: str | None, now=None, max_minutes: int = 120) -> bool:
    """①併合を許す鮮度か (直前 turn が max_minutes 以内)。
    ★cross-check DA high: 併合に時刻境界が無いと、数日前の「昨日の売上」に対する今日の
    「日本の」が **今日基準の昨日** で解決され、別の日のデータが継続の顔で注入される。
    ts 不明 (None/壊れ) は False = fail-closed (併合しない = 修正前の安全な挙動)。"""
    if not prev_ts_iso:
        return False
    from datetime import datetime
    try:
        prev = datetime.fromisoformat(prev_ts_iso)
    except Exception:
        return False
    now = now or datetime.now().astimezone()
    if prev.tzinfo is None and now.tzinfo is not None:
        prev = prev.replace(tzinfo=now.tzinfo)
    delta = (now - prev).total_seconds()
    return 0 <= delta <= max_minutes * 60


def merge_followup_query(message: str, prev_user_query: str) -> str:
    """①follow-up 併合 — 直前が業務データ照会で、今回が**短い継続発話** (「先週は?」「日本の」
    「客数は?」) の時だけ、前クエリの**日付とメトリック語だけ**を補完した effective query を
    返す。それ以外は message をそのまま返す。run_agent (main.py `_biz_follow`) 実証ロジックの
    pure 化+強化 = LLM 不使用。

    ★移植時に見つけた移植元の弱点 2 つの修正:
    (1) 日付だけの引き継ぎだと「日本の」型は売上語を失い全 injector の _SALES_RE ゲートで
        不発 → メトリック語 (売上/客数/客単価/前年比) も引き継ぐ。
    (2) 「客数は?」型は業務語を含むため直判定で素通りし日付が落ちる → 短い継続なら業務語
        込みでも日付を補完 (ただし今回に日付/別期間語がある時は補完しない)。
    次元 (エリア/業態/国名) は引き継がない = 今回の次元指定を優先 (次元シャドウ防止)。
    既に含む語は二重併合しない (run_agent pre-route が併合済み effective を渡してくる経路の
    安全弁 — その経路もこの併合で売上語が補われて治る)。
    """
    m = (message or "").strip()
    p = (prev_user_query or "").strip()
    if not m or not p:
        return message
    if _PLACEHOLDER_PREV_RE.match(p):    # system 生成 placeholder は継続元にしない
        return message
    if not is_business_data_query(p):    # 直前が業務照会でない → 継続ではない
        return message
    if len(m) > 25 or _NOT_BUSINESS_RE.search(m):   # 長文/雑談は独立質問として通常フロー
        return message
    # 短い継続に見えるか: 業務語そのもの (客数は? 既存店は?) or 継続語 (日本の エリア別で)
    if not (is_business_data_query(m) or is_business_followup(m)):
        return message
    carry: list[str] = []
    dp = extract_date_phrase(p)
    if (dp and not _DATE_PHRASE_RE.search(m)        # 今回に日付が無く
            and not _OTHER_PERIOD_RE.search(m)):    # 別期間語 (今月 等) も無い時だけ日付補完
        carry.append(dp)
    if not _METRIC_CARRY_RE.search(m):   # メトリック語 (今回に無い時だけ前クエリから)
        mm = _METRIC_CARRY_RE.search(p)
        if mm:
            carry.append(mm.group(0))
    if not carry:
        return message
    return f"{' '.join(carry)} {m}"


# ★cross-check DA high: ②の gate は「injector が実際に serve できるメトリック」に限る。
#   _BIZ_SALES_RE (達成率/ランキング/SPH/日販/実績/粗利…) を流用すると、データ源が無く
#   **構造的に永遠に serve 不能**な質問クラスで毎回 slot LLM が空振りし、+1 call と待ちが常設化する
#   (規程 intent を除外したのと同じ理由)。daily_history_inject._SALES_RE ∪ yoy トリガ相当に一致させる。
_SERVICEABLE_METRIC_RE = re.compile(r"売り?上げ?|売上|客数|客単価|前年比|昨対|昨年対比|既存店")
# メトリック語を含んでいても injector が形として serve できない要求 (店舗別ランキング等)。
# 「売上ランキング」は 売上 を含むが、injector は国/エリア/業態/リーグ集計しか持たない。
_NON_SERVICEABLE_SHAPE_RE = re.compile(r"ランキング|順位|トップ\s*\d|ベスト\s*\d|上位\s*\d|ワースト")


def merge_from_dm_records(query: str, records: list | None, *, now=None,
                          max_minutes: int = 120) -> str:
    """①の判断一式 (直前 user turn の選択 → 鮮度 → placeholder → 併合) を pure 化。

    records = clone_history.load_recent(..., scope="dm", with_ts=True) の戻り
    ([{"role","content","ts"}, ...] 時系列昇順)。呼び手 (clone_respond_public) は DM のみで使う。
    ★ここを inline にしていると夜間 regression (history=[]・user_id 無し) が新経路を通らず
    「空 PASS」になる = cross-check DA 指摘。pure 化してテストで固定する。
    """
    q = (query or "").strip()
    if not q or not records:
        return query
    users = [r for r in records if isinstance(r, dict) and r.get("role") == "user"]
    # 現在の発話は既に append 済のことがある (LW DM 経路) → content 一致で除外
    if users and (users[-1].get("content") or "").strip() == q:
        users = users[:-1]
    if not users:
        return query
    prev = users[-1]
    if not is_fresh_followup(prev.get("ts"), now=now, max_minutes=max_minutes):
        return query                      # 古い/不明 = 併合しない (時制ずれ防止・fail-closed)
    return merge_followup_query(query, prev.get("content") or "")


def is_sales_intent(message: str) -> bool:
    """②スロット解釈 fallback の発火 gate。**既存 injector が消化できるメトリック**のみ。
    ★DA 指摘 2 点を反映: (1) 規程質問は売上 injector が無く恒常 miss → 除外
    (2) 達成率/ランキング/SPH/日販/実績 等も injector 非対応 → 除外 (空振り LLM の常設化を防ぐ)。"""
    m = (message or "").strip()
    if not m or _NOT_BUSINESS_RE.search(m):
        return False
    if _NON_SERVICEABLE_SHAPE_RE.search(m):   # 形として serve 不能 (ランキング等)
        return False
    return bool(_SERVICEABLE_METRIC_RE.search(m))


def slots_conflict_with_query(source_query: str, slots: dict | None) -> bool:
    """★cross-check DA: 元クエリに**既知の期間語**があるのに slot の period がそれと違う場合、
    決定論パーサが既に拾えているはずの語なので、この不一致は LLM 誤抽出の強いシグナル。
    (「一昨日」→「昨日」のような誤解釈で、別の日の正しい数値を自信満々に返す事故を防ぐ)"""
    if not slots or not slots.get("period"):
        return False
    known = _DATE_PHRASE_RE.findall(source_query or "")
    if not known:
        return False
    return slots["period"] not in known


_SLOT_KEYS = ("period", "scope", "dimension")
# スロット値の保守的な文字集合 (漢字/かな/カナ/英数 + 期間の記号)。20 字超・記号混入は破棄。
_SLOT_VALUE_RE = re.compile(r"^[0-9A-Za-z一-龥ぁ-んァ-ヶー々/月日週年比別]{1,20}$")
# dimension は既存パーサが解釈できる語だけの whitelist (junk 混入を構造で遮断)。
_SLOT_DIMENSIONS = frozenset({
    "国別", "エリア別", "業態別", "リーグ別", "既存店", "全店",
    "客数", "客単価", "前年比", "既存店前年比", "昨対", "昨年対比",
})


def parse_slot_json(text: str | None) -> dict | None:
    """②スロット抽出 LLM 出力の防御的 parse。{period, scope, dimension} の str のみ採用、
    period/scope は _SLOT_VALUE_RE (保守的文字集合・≤20字)、dimension は whitelist のみ。
    壊れた出力 / 全 null は None。
    ★ここは信頼境界: LLM 出力はデータ扱いで、通った値も最終的には既存の決定論パーサ
    (daily_history_inject / yoy_inject) が validator になる = 数値はここを通れない。"""
    if not text:
        return None
    m = re.search(r"\{.*?\}", text, re.S)
    if not m:
        return None
    try:
        raw = json.loads(m.group(0))
    except Exception:
        return None
    if not isinstance(raw, dict):
        return None
    out: dict = {}
    for k in _SLOT_KEYS:
        v = raw.get(k)
        if isinstance(v, str):
            v = v.strip()
            if not v or v.lower() in ("null", "none"):
                continue
            if k == "dimension":
                if v in _SLOT_DIMENSIONS:
                    out[k] = v
            elif _SLOT_VALUE_RE.match(v):
                out[k] = v
    return out or None


_METRIC_HINT_RE = re.compile(r"売上|客数|客単価|前年|昨対|YoY|yoy")


def compose_slot_query(slots: dict | None, *, source_query: str = "") -> str | None:
    """②スロット → 既存決定論パーサが読める canonical クエリ文字列を合成。
    period 必須 (日付の無い売上話にデータ注入しない = 捏造防止の既存原則を維持)。
    ★数値は一切含まない — LLM が出すのは語スロットだけで、値は既存 injector が
    canonical wiki から決定論で出す (桁事故が構造的に不可能)。

    ★cross-check DA high: メトリック語の補完は **元クエリに実在する語をそのまま使う**。
    無条件に「の売上」を付けると、ユーザが聞いたメトリック (客数/客単価) を売上にすり替えた
    注入になる (「違う質問への正しい数値」を命令口調で押し込む事故)。元クエリに serve 可能な
    メトリックが無ければ合成しない (None)。"""
    if not slots:
        return None
    period = slots.get("period")
    if not period:
        return None
    parts = [period]
    if slots.get("scope"):
        parts.append(slots["scope"])
    if slots.get("dimension"):
        parts.append(slots["dimension"])
    q = "の".join(parts)
    if not _METRIC_HINT_RE.search(q):
        mm = _SERVICEABLE_METRIC_RE.search(source_query or "")
        if not mm:
            return None                  # 元クエリのメトリックが不明 → 勝手に売上にしない
        q += "の" + mm.group(0)
    return q
