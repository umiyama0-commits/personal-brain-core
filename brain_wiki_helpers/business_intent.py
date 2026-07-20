"""brain_wiki_helpers/business_intent.py — 業務データ質問の決定論判定
(★2026-07-20 §1.15 cross-check: 売上の数字は必ず canonical+guard を通すため、agent の
未ガード経路に落とさず clone 回答エンジンへ pre-route する判定を pure function で分離)。

main.py (fastapi 依存) から切り出して単体テスト可能に (§1.12b: ロジックは helper へ)。
high-recall 寄り: 取りこぼすと未ガードで答えてしまうため業務データっぽいものは広めに拾う。
誤って personal を業務判定しても clone は public scope の答えを返すだけ (実害小)。
"""
from __future__ import annotations

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
    r"国別|業態|リーグ|店|前年|昨年|前月|同日|曜日|既存|全店|内訳|別で?$"
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
