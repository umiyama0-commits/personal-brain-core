"""yoy — 前年比を社内慣習の ratio 表記 (前年=100%) で文字列化する純関数。

★2026-06-11 海山指示: 前年比は「+50%」でなく「150%」と社内で表現する。
delta (+X%) ではなく ratio (前年=100% 基準) で統一する。判定基準も同じ目線で、
owndays-sales-metrics-stance.md の表 (120%以上=すごい … 100%未満=要対応) と整合。
Monday Dash 既存店前年比 (= build_monday_dash_latest.py、売上138% 等) も同じ ratio 慣習で、
本 helper はそこへ brain_wiki.py の店舗履歴 YoY を揃える (= 唯一 delta だった箇所の統一)。
"""
from __future__ import annotations


def format_yoy_ratio(curr: float, prev: float) -> str:
    """前年比を ratio 表記の文字列で返す。

    前年=100% 基準:
      - 153.4% = 前年の 1.534 倍 (= 旧表記 +53.4%)
      - 99.1%  = 前年割れ (= 旧表記 -0.9%、100 未満で減を表す)
      - 100.0% = 前年同水準
    prev <= 0 (前年データ無し / ゼロ) は "前年比 N/A" (ゼロ除算回避)。
    ★生の数値から 1 回だけ算出する (delta 経由の 100+x にすると二重丸めの恐れ)。
    """
    if prev <= 0:
        return "前年比 N/A"
    ratio = curr / prev * 100
    return f"前年比 {ratio:.1f}%"
