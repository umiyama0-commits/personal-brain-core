"""scripts/style_reflux.py — audit / feedback / 発見 → style 逆流 pipeline

★2026-05-26 海山 B1+B3「audit fail / feedback の style 逆流」 (= 品質改善 top 3 の 1 つ):
直近 30 日の失敗 pattern を集約 → 頻出 type を抽出 → style wiki 改善 proposal を生成。

集計 source:
1. clone_audit (= 海山 audit) — verdict=bad/fix の note (= 修正コメント)
2. clone_feedback (= 社員修正希望) — feedback (= 何が違う)
3. clone_learning (= LLM auto-discovery) — category=response_quality の insight + patch
   (= 既に LLM が「too_passive / too_verbose / tone mismatch / factual」 等で分類済)

出力:
- data/brain/clone_improve/style_reflux/YYYY-MM-DD.md (= 週次レポート、~5-10KB)
  * 頻出 failure pattern top 10
  * 各 pattern の 代表 example 2-3 件 (= 海山が「これは確かに直すべき」 判断材料)
  * 提案 style wiki 追記 (= style-no-claude-proposals.md / style-response-examples.md 等)
- LINE Push (= 海山に「style 逆流レポート出た」 通知)

usage:
  python3 scripts/style_reflux.py             # 直近 30 日
  python3 scripts/style_reflux.py --days 14   # 期間変更
  python3 scripts/style_reflux.py --dry-run   # 出力 print のみ

cron: 月曜 04:10 (= quality_metrics 04:05 後)
"""
from __future__ import annotations

import argparse
import json
import logging
import os
import re
import sys
from collections import Counter, defaultdict
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Optional

logger = logging.getLogger("style_reflux")
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")

JST = timezone(timedelta(hours=9))
APP_ROOT = Path(os.getenv("BRAIN_APP_ROOT", "/app"))
BRAIN_ROOT = Path(os.getenv("BRAIN_ROOT", str(APP_ROOT / "data" / "brain")))

AUDIT_DIR = BRAIN_ROOT / "clone_audit"
FEEDBACK_DIR = BRAIN_ROOT / "clone_feedback"
LEARNING_DIR = BRAIN_ROOT / "clone_learning"

OUT_DIR = BRAIN_ROOT / "clone_improve" / "style_reflux"

sys.path.insert(0, str(Path(__file__).resolve().parent))
try:
    from clone_improve_lib import line_push, line_push_digest  # type: ignore
except Exception as e:
    logger.warning(f"clone_improve_lib import failed: {e}")
    def line_push(text: str) -> bool:  # type: ignore
        logger.error(f"[LINE PUSH stub] {text}")
        return False
    def line_push_digest(text: str, component: str = "") -> bool:  # type: ignore
        return line_push(text)


def _iter_jsonl(path: Path):
    if not path.exists():
        return
    try:
        for line in path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                yield json.loads(line)
            except Exception:
                continue
    except Exception as e:
        logger.warning(f"failed to read {path.name}: {e}")


def _parse_ts(s: str) -> Optional[datetime]:
    try:
        return datetime.fromisoformat(s)
    except Exception:
        return None


# ─── failure pattern keyword (= 既存 LLM auto-discovery の分類 + 海山経験則) ──
PATTERN_KEYWORDS: dict[str, list[str]] = {
    "too_passive": [
        "データ無い", "データがない", "分からない", "答えられない",
        "持っていない", "情報がない", "too_passive", "謙遜しすぎ", "引きすぎ",
    ],
    "too_verbose": [
        "長すぎ", "冗長", "too_verbose", "too_long", "短く", "簡潔",
    ],
    "tone_mismatch": [
        "tone", "口調", "ビジネス調", "硬い", "AI 臭", "ai_smell",
        "教科書", "網羅的", "5 bullet",
    ],
    "factual_error": [
        "数字違", "数字が違", "間違っ", "誤り", "factual",
        "事実誤", "wrong", "incorrect", "hallucin",
    ],
    "missed_context": [
        "前回", "文脈", "context", "anaphora", "あの店舗",
        "覚えてない", "覚えていない", "話を聞いてない",
    ],
    "wrong_default": [
        "default が違", "scope", "intent", "意図が違", "聞いてる事が違",
        "海外じゃなく", "日本じゃなく", "今月じゃなく", "今日じゃなく",
    ],
    "mirroring_fail": [
        "ミラーリング", "mirror", "雑談 query に", "テンポ",
    ],
}


def classify_text(text: str) -> str:
    """text を keyword で pattern 分類. 該当無ければ 'other'."""
    text_lower = (text or "").lower()
    for pat, kws in PATTERN_KEYWORDS.items():
        for kw in kws:
            if kw.lower() in text_lower:
                return pat
    return "other"


def collect_audit_fails(days: int = 30) -> list[dict]:
    """clone_audit から verdict=bad/fix の note を収集."""
    cutoff = datetime.now(JST) - timedelta(days=days)
    items = []
    if not AUDIT_DIR.exists():
        return items
    for f in sorted(AUDIT_DIR.glob("*.jsonl"), reverse=True):
        for r in _iter_jsonl(f):
            ts = _parse_ts(r.get("ts", "") or r.get("audited_at", ""))
            if ts and ts.astimezone(JST) < cutoff:
                continue
            verdict = r.get("verdict", "")
            if verdict not in ("bad", "fix"):
                continue
            note = (r.get("note") or "").strip()
            items.append({
                "source": "audit",
                "ts": r.get("ts", "")[:16],
                "verdict": verdict,
                "user_query": (r.get("user_query") or "")[:200],
                "bot_response": (r.get("bot_response") or "")[:300],
                "note": note,
                "pattern": classify_text(note + " " + (r.get("user_query") or "")),
            })
    return items


def collect_feedback(days: int = 30) -> list[dict]:
    """clone_feedback から user feedback を収集."""
    cutoff = datetime.now(JST) - timedelta(days=days)
    items = []
    if not FEEDBACK_DIR.exists():
        return items
    for f in sorted(FEEDBACK_DIR.glob("*.jsonl"), reverse=True):
        for r in _iter_jsonl(f):
            ts = _parse_ts(r.get("timestamp", ""))
            if ts and ts.astimezone(JST) < cutoff:
                continue
            feedback = (r.get("feedback") or "").strip()
            if not feedback:
                continue
            items.append({
                "source": "feedback",
                "ts": r.get("timestamp", "")[:16],
                "user_query": (r.get("trigger_msg") or "")[:200],
                "bot_response": (r.get("response") or "")[:300],
                "note": feedback,
                "pattern": classify_text(feedback),
            })
    return items


def collect_learning_rq(days: int = 30) -> list[dict]:
    """clone_learning の category=response_quality (= LLM 抽出済) を収集."""
    cutoff = datetime.now(JST) - timedelta(days=days)
    items = []
    if not LEARNING_DIR.exists():
        return items
    for f in sorted(LEARNING_DIR.glob("*.jsonl"), reverse=True):
        for r in _iter_jsonl(f):
            ts = _parse_ts(r.get("timestamp", ""))
            if ts and ts.astimezone(JST) < cutoff:
                continue
            if r.get("category") != "response_quality":
                continue
            insight = (r.get("insight") or "").strip()
            patch = (r.get("proposed_wiki_patch") or "").strip()
            # insight に "[too_passive]" 等の prefix tag がついてる
            tag_match = re.match(r"^\[([a-z_]+)\]", insight)
            pattern = tag_match.group(1) if tag_match else classify_text(insight)
            items.append({
                "source": "learning",
                "ts": r.get("timestamp", "")[:16],
                "status": r.get("status", ""),
                "user_query": (r.get("source_snippet") or "")[:200],
                "note": insight,
                "patch": patch,
                "pattern": pattern,
            })
    return items


def aggregate_patterns(all_items: list[dict]) -> dict[str, list[dict]]:
    """全 item を pattern 別に group + 頻度 sort."""
    by_pattern: dict[str, list[dict]] = defaultdict(list)
    for it in all_items:
        by_pattern[it["pattern"]].append(it)
    # 各 pattern 内も新しい順
    for p in by_pattern:
        by_pattern[p].sort(key=lambda x: x.get("ts", ""), reverse=True)
    return dict(by_pattern)


def generate_proposals(pattern: str, examples: list[dict]) -> str:
    """1 pattern の改善 proposal text を生成 (= MVP は決定論的、後で LLM に拡張可)."""
    n = len(examples)
    if pattern == "too_passive":
        return (
            f"**too_passive** ({n} 件)\n"
            f"- 検出: bot が「データ無い」「分からない」と引きすぎ。\n"
            f"- 対策案: `data/brain/wiki/style/style-no-claude-proposals.md` に counter-example 追記、\n"
            f"  「retrieval block に該当無し時の正しい answer 例」 を 2-3 件 増量。\n"
            f"  特に retrieval 0 件 fallback prompt を強化検討。"
        )
    elif pattern == "too_verbose":
        return (
            f"**too_verbose** ({n} 件)\n"
            f"- 検出: 応答が query に対し過剰に長い。\n"
            f"- 対策案: `style-response-examples.md` の「短い query には短い response」例追加、\n"
            f"  CLONE_PUBLIC_PROMPT に「query 文字数の 5-8 倍超えない」 rule 明示。"
        )
    elif pattern == "tone_mismatch":
        return (
            f"**tone_mismatch** ({n} 件)\n"
            f"- 検出: AI 臭・教科書的・ビジネス調 で 海山フレーバー欠如。\n"
            f"- 対策案: `style.md` の口語例増量、`style-response-examples.md` の 雑談 sample 追加、\n"
            f"  応答品質 judge の ai_smell スコア低 turn を pinned counter-example 化。"
        )
    elif pattern == "factual_error":
        return (
            f"**factual_error** ({n} 件)\n"
            f"- 検出: 数字違い / 事実誤認。\n"
            f"- 対策案: retrieval block の数字を「そのまま」 quote する rule 強化、\n"
            f"  CLONE_PUBLIC_PROMPT に「retrieval に無い数字は絶対に答えない」明示。\n"
            f"  該当 query type は wiki 拡充 (= 該当 data を pre-compute index 化)。"
        )
    elif pattern == "missed_context":
        return (
            f"**missed_context** ({n} 件)\n"
            f"- 検出: 前回会話・前提を拾えてない。\n"
            f"- 対策案: clone_memory の Ongoing 反映 改善、anaphora 解決の仕組み追加 (Phase 2)、\n"
            f"  同 user の直近 N turn を retrieval prefix に追加検討。"
        )
    elif pattern == "wrong_default":
        return (
            f"**wrong_default** ({n} 件)\n"
            f"- 検出: scope (= 日本 / 海外 / 今月 / 今日) の default 解釈ズレ。\n"
            f"- 対策案: 既存 query intent 検出 拡張 (= brain_wiki.py _detect_query_intent)、\n"
            f"  ambiguous query の clarifying question を許可 (= CLONE_PUBLIC_PROMPT rule 追加)。"
        )
    elif pattern == "mirroring_fail":
        return (
            f"**mirroring_fail** ({n} 件)\n"
            f"- 検出: ミラーリング失敗 (= 温度・テンポ・量のミスマッチ)。\n"
            f"- 対策案: `style-response-examples.md` のミラーリング good/bad 並べ、\n"
            f"  response_quality_judge の mirroring_fit スコア低 turn を pinned 化。"
        )
    else:  # other
        return (
            f"**other / unclassified** ({n} 件)\n"
            f"- 検出: pattern 分類 keyword に該当しない failure。\n"
            f"- 対策案: 海山が中身確認、新 pattern keyword を PATTERN_KEYWORDS に追加 or\n"
            f"  個別 item を `/admin/review/learning` から処理。"
        )


def build_report(by_pattern: dict[str, list[dict]],
                 days: int, today: date) -> str:
    """週次レポート markdown を組み立て."""
    total = sum(len(v) for v in by_pattern.values())
    # 頻度順 sort
    pattern_order = sorted(by_pattern.keys(), key=lambda p: -len(by_pattern[p]))

    parts = [
        f"---",
        f"date: {today.isoformat()}",
        f"window_days: {days}",
        f"type: style_reflux_weekly",
        f"---",
        f"# Style 逆流 週次レポート ({today.isoformat()}、直近 {days} 日)",
        "",
        f"**集計**: audit + feedback + learning queue から計 **{total} 件** の failure pattern。",
        f"**頻度順 top {min(10, len(pattern_order))}** の patterns を以下にまとめ、各々の 改善 proposal を提示。",
        f"",
        f"## 📊 pattern frequency",
        f"",
        f"| # | pattern | count | 主 source |",
        f"|---|---|---|---|",
    ]

    for i, p in enumerate(pattern_order[:10], start=1):
        items = by_pattern[p]
        source_counts = Counter(it["source"] for it in items)
        src_str = ", ".join(f"{k}={v}" for k, v in source_counts.most_common())
        parts.append(f"| {i} | `{p}` | {len(items)} | {src_str} |")
    parts.append("")

    # 各 pattern の detail
    for i, p in enumerate(pattern_order[:10], start=1):
        items = by_pattern[p]
        parts.append(f"## {i}. {p} ({len(items)} 件)")
        parts.append("")
        parts.append(generate_proposals(p, items))
        parts.append("")
        parts.append(f"### 代表 example (= 直近 2 件)")
        for it in items[:2]:
            parts.append(f"- **[{it['source']}/{it.get('ts','?')}]**")
            uq = (it.get("user_query") or "")[:150]
            if uq:
                parts.append(f"  - USER: `{uq}`")
            br = (it.get("bot_response") or "")[:200]
            if br:
                parts.append(f"  - BOT: `{br}`")
            note = (it.get("note") or "")[:200]
            if note:
                parts.append(f"  - note/insight: {note}")
        parts.append("")

    parts.append(f"---")
    parts.append(f"自動生成: `scripts/style_reflux.py`、再生成 = 月曜 04:10 cron")
    parts.append(f"")
    parts.append(f"次 step: 海山が上記 proposal を見て、各 pattern について style wiki 追記の判断。")
    parts.append(f"具体 wiki edit は `/admin/review/learning` (= 直接入力 mode) で記録すると追跡可。")

    return "\n".join(parts)


def run_once(days: int = 30, dry_run: bool = False) -> dict:
    """1 回 集計 + 出力 + LINE Push."""
    today = (datetime.now(JST)).date()
    logger.info(f"collecting failure patterns (past {days} days)...")

    audit = collect_audit_fails(days)
    feedback = collect_feedback(days)
    learning = collect_learning_rq(days)
    all_items = audit + feedback + learning
    logger.info(f"  audit={len(audit)}, feedback={len(feedback)}, learning_rq={len(learning)}, total={len(all_items)}")

    by_pattern = aggregate_patterns(all_items)
    report = build_report(by_pattern, days, today)

    if dry_run:
        print(report[:3000])
        print(f"\n... (full report length: {len(report)} chars)")
        return {
            "items": len(all_items), "patterns": len(by_pattern),
            "report_length": len(report), "dry_run": True,
        }

    OUT_DIR.mkdir(parents=True, exist_ok=True)
    out_file = OUT_DIR / f"{today.isoformat()}.md"
    out_file.write_text(report, encoding="utf-8")
    logger.info(f"saved: {out_file} ({len(report)} chars)")

    # LINE Push (= 1 line summary)
    if all_items:
        top3 = sorted(by_pattern.items(), key=lambda x: -len(x[1]))[:3]
        top3_str = ", ".join(f"{p}={len(v)}" for p, v in top3)
        line_push_digest(
            f"📊 [Umiyama AI Agent] style 逆流 週次レポート ({today.isoformat()})\n"
            f"直近 {days} 日 failure {len(all_items)} 件、頻出 top 3: {top3_str}\n"
            f"詳細: data/brain/clone_improve/style_reflux/{today.isoformat()}.md\n"
            f"or /admin/review/style-reflux?token=...",
            "style逆流",
        )

    return {
        "items": len(all_items), "patterns": len(by_pattern),
        "report_file": str(out_file), "report_length": len(report),
    }


def main():
    ap = argparse.ArgumentParser(description="style 逆流 pipeline (★2026-05-26 海山 B1+B3)")
    ap.add_argument("--days", type=int, default=30, help="集計対象期間 (default 30 日)")
    ap.add_argument("--dry-run", action="store_true", help="出力 print のみ、ファイル + Push せず")
    args = ap.parse_args()
    result = run_once(days=args.days, dry_run=args.dry_run)
    print(json.dumps(result, ensure_ascii=False, default=str))


if __name__ == "__main__":
    main()
