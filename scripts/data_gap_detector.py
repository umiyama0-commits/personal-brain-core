"""scripts/data_gap_detector.py — bot 応答中の「データ無し」発言を検出

★2026-05-26 海山指示:
1. 「データ無い」回答 tone を「今後拡充予定」 wording に転換 (= prompt rule 2a 改訂で対応)
2. 該当回答を queue 化 → ダッシュボードで「データ拡充候補」 として review

このモジュールは検出のみ。検出時に bot_events に `data_gap_detected` event を emit、
集約は services/data_gaps.py + 週次 aggregator で。

usage:
  from scripts.data_gap_detector import detect_data_gap
  info = detect_data_gap(user_query, bot_response)
  if info:
      log_bot_event("clone_respond", "data_gap_detected", ...)
"""
from __future__ import annotations

import re
from typing import Optional

# 「データ無い」「分からない」系の発言 pattern
# 重要: bot の発言中の表現を検出する、user query は対象外。
DATA_GAP_PATTERNS: list[tuple[re.Pattern, str]] = [
    # data 系
    (re.compile(r"データ\s*(?:が|は)?\s*(?:(?:無|な)い|ありませ[んょ]|無いです)"), "no_data"),
    (re.compile(r"情報\s*(?:が|は)?\s*(?:(?:無|な)い|ありませ[んょ]|無いです)"), "no_info"),
    (re.compile(r"記録\s*(?:が|は)?\s*(?:(?:無|な)い|ありませ[んょ]|無いです)"), "no_record"),
    # 把握 / 持つ
    (re.compile(r"(?:把握|確認|理解)\s*(?:できて\s*(?:い)?\s*(?:ない|ません)|してい?(?:ない|ません))"), "not_grasped"),
    (re.compile(r"持って\s*(?:い)?\s*(?:ない|いません|ません)"), "not_held"),
    # 海山フレーバー
    (re.compile(r"流し込めて\s*(?:い)?\s*(?:ない|ません)"), "not_ingested"),
    # 一般 negation (= 申し訳ありません は除外する必要あり、context check)
    (re.compile(r"分か(?:ら|り)\s*(?:ません|ない|ず)"), "dunno"),
    (re.compile(r"答えられ\s*(?:ません|ない)"), "cant_answer"),
    (re.compile(r"(?:整備|集計|蓄積)\s*(?:中|され?て\s*(?:い)?\s*(?:ない|ません))"), "not_aggregated"),
]

# 「既に新 tone」を示唆する keyword (= 検出は続けるが、severity を warning→info に下げる材料)
FORWARD_LOOKING_KEYWORDS = [
    "今後", "拡充", "更新する予定", "集めて", "取り行こう", "取りに行こう",
    "候補に上げ", "候補に上げとく", "優先度上げ", "整備中", "整備されて",
]


def detect_data_gap(user_query: str, bot_response: str) -> Optional[dict]:
    """bot 応答中の「データ無し」発言を検出.

    Returns:
        matched 時: {
            "matched_text": str,        # 該当 pattern にヒットした text
            "category": str,            # 'no_data' / 'no_info' / ... (= pattern label)
            "snippet": str,             # 周辺 60 字
            "forward_looking": bool,    # 既に「今後拡充」 tone を含むか
            "position": int,            # 応答内の matched 位置 (offset)
        }
        無ければ None
    """
    if not bot_response:
        return None

    # 最初に hit した pattern を採用 (= 複数 hit でも 1 event/turn)
    for pat, category in DATA_GAP_PATTERNS:
        m = pat.search(bot_response)
        if not m:
            continue
        start = max(0, m.start() - 30)
        end = min(len(bot_response), m.end() + 30)
        snippet = bot_response[start:end]
        forward = any(kw in bot_response for kw in FORWARD_LOOKING_KEYWORDS)
        return {
            "matched_text": m.group(),
            "category": category,
            "snippet": snippet,
            "forward_looking": forward,
            "position": m.start(),
        }
    return None
