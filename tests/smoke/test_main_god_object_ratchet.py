"""main.py の god object 逆進行を止める ratchet (★2026-07-10 世界基準評価 S4a)。

CLAUDE.md §1.12b:「main.py に新規 endpoint / handler を足さない」の**機械的 enforcement**。
前回評価で指摘後もむしろ +613 行成長した (計画だけの strangler は太り続ける、Fowler の失敗型)。
新規 endpoint は routes/ の APIRouter へ、新規ロジックは services/ or brain_wiki_helpers/ へ。

ratchet: main.py の route decorator 数・行数が **baseline を超えたら fail**。
- 減る分には baseline を下げてよい (strangler で routes/ へ移設したら下方更新)。
- 増やす PR は「main.py に足すな」= このテストで赤 → routes/ へ回す動機付け。

import-linter で中枢 import 循環 (brain_commands⇄main⇄routes) を切るのは別タスク
(既存循環を先に解消しないと CI が即赤になるため、まず ratchet で流入を止める)。
"""
import pathlib
import re

ROOT = pathlib.Path(__file__).resolve().parents[2]

# baseline (2026-07-10 実測)。**endpoint 数が hard ratchet** (新規 API surface = god object の本質。
# 上げるのは原則禁止=移設で減ったら下げる)。line 数は「大量投下」検知用の緩い天井 (小さな wiring
# 追加=bug fix や services 委譲の呼出しは許容、feature 丸ごと投下だけ止める)ため実数+余裕を持たせる。
# 49 = /store-survey 復活分 (acf05f2 revert: AM が入力中の実運用フォームを誤削除→復旧。
# 新規 feature でなく既存 surface の restoration)。次の下げ先: store-survey 系を routes/ へ移設。
MAX_ENDPOINTS = 49
# 現 7981 (2026-07-20 売上フォローアップ対応の pre-route 拡張 = 「日本の」型を直前売上質問と
# 併合して clone へ。YoY/日本認識ロジックは helper 分離、main.py は wiring のみ)。
# line 数は「大量投下検知」の緩い天井 = 小 wiring は実数に追随。
MAX_LINES = 7997  # ★2026-08-03: web-config の深層人格 平文露出の封鎖 (+13 行の wiring のみ。
# interview 絞り込み / 要約遮断 / personal 話題除外の 3 判定はいずれも
# brain_wiki_helpers/voice_visibility.py 側の pure function に置き、main.py は呼ぶだけ。
# endpoint 数は 49 で不変 = hard ratchet は動かしていない)

_ROUTE_RE = re.compile(r"^@app\.(get|post|put|delete|patch|websocket)\(", re.MULTILINE)


def _main_src() -> str:
    return (ROOT / "main.py").read_text(encoding="utf-8")


def test_main_endpoint_count_does_not_grow():
    src = _main_src()
    n = len(_ROUTE_RE.findall(src))
    assert n <= MAX_ENDPOINTS, (
        f"main.py の endpoint が {n} 個 (baseline {MAX_ENDPOINTS} 超過)。"
        "新規 endpoint は routes/ の APIRouter へ (CLAUDE.md §1.12b、god object 逆進行の停止)。"
        "移設で減った場合は MAX_ENDPOINTS を実数に下げる。"
    )


def test_main_line_count_does_not_grow():
    n = len(_main_src().splitlines())
    assert n <= MAX_LINES, (
        f"main.py が {n} 行 (baseline {MAX_LINES} 超過)。新規ロジックは services/ or "
        "brain_wiki_helpers/ の pure function へ (CLAUDE.md §1.12b)。移設で減ったら baseline を下げる。"
    )
