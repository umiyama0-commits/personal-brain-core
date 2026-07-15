"""
clone_weekly_report.py — うみやまAI 週次改善レポート

過去 7 日の auto_edit_log + drafts + daily metrics を読んで、海山が 3 分で読める
markdown レポートを生成。LINE Push で要点通知。

cron: 月曜 09:00 JST
  python3 scripts/clone_weekly_report.py
"""
from __future__ import annotations

import asyncio
import json
import logging
import sys
from datetime import datetime, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from clone_improve_lib import (
    ensure_dirs, call_llm, read_jsonl, line_push, supervisor_model,
    IMPROVE_DIR, DRAFTS_DIR, REPORTS_DIR, METRICS_DIR, AUTO_EDIT_LOG, JST,
)

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger("clone_weekly_report")


def load_week_data(end_date: datetime, days: int = 7):
    start = end_date - timedelta(days=days)

    # 1) auto_edit_log
    all_edits = read_jsonl(AUTO_EDIT_LOG)
    week_edits = []
    for e in all_edits:
        try:
            ts = datetime.fromisoformat(e.get("timestamp", "").replace("Z", "+00:00"))
            if start <= ts < end_date:
                week_edits.append(e)
        except Exception:
            continue

    # 2) drafts (今週新規)
    drafts = []
    for d in DRAFTS_DIR.rglob("*.md"):
        try:
            mtime = datetime.fromtimestamp(d.stat().st_mtime, tz=JST)
            if start <= mtime < end_date:
                drafts.append({
                    "path": str(d.relative_to(IMPROVE_DIR)),
                    "content": d.read_text(encoding="utf-8")[:1500],
                })
        except Exception:
            continue

    # 3) daily metrics
    week_metrics = []
    cur = start
    while cur < end_date:
        p = METRICS_DIR / f"{cur.strftime('%Y-%m-%d')}.json"
        if p.exists():
            try:
                week_metrics.append(json.loads(p.read_text(encoding="utf-8")))
            except Exception:
                pass
        cur += timedelta(days=1)

    # 4) 前週 metrics (比較用)
    prev_start = start - timedelta(days=days)
    prev_metrics = []
    cur = prev_start
    while cur < start:
        p = METRICS_DIR / f"{cur.strftime('%Y-%m-%d')}.json"
        if p.exists():
            try:
                prev_metrics.append(json.loads(p.read_text(encoding="utf-8")))
            except Exception:
                pass
        cur += timedelta(days=1)

    return week_edits, drafts, week_metrics, prev_metrics


def agg_metrics(metrics_list: list[dict]) -> dict:
    if not metrics_list:
        return {}
    out = {
        "total_conversations": 0,
        "total_turns": 0,
        "unique_users": set(),
        "deep_sessions": 0,
        "one_shot_sessions": 0,
        "knowledge_gap_count": 0,
        "ai_turn_count": 0,
        "user_turn_count": 0,
        "satisfaction_signals": 0,
        "abandon_count": 0,
        "session_count": 0,
        "correction_count": 0,
        "rephrase_count": 0,
        "power_users": set(),
        "new_users": set(),
    }
    for m in metrics_list:
        v = m.get("volume", {})
        d = m.get("depth", {})
        q = m.get("quality", {})
        u = m.get("users", {})
        out["total_conversations"] += v.get("total_conversations", 0)
        out["total_turns"] += v.get("total_turns", 0)
        # unique_users は近似 (日次 set を merge できないので件数合計の min)
        out["deep_sessions"] += d.get("deep_sessions", 0)
        out["one_shot_sessions"] += d.get("one_shot_sessions", 0)
        out["session_count"] += v.get("total_conversations", 0)
        # rates → 件数推定
        sess = v.get("total_conversations", 0)
        turn = v.get("total_turns", 0)
        out["knowledge_gap_count"] += int(q.get("knowledge_gap_rate", 0) * sess)
        out["correction_count"] += int(q.get("correction_rate", 0) * turn)
        out["rephrase_count"] += int(q.get("rephrase_retry_rate", 0) * turn)
        out["satisfaction_signals"] += q.get("satisfaction_signals", 0)
        out["abandon_count"] += int(d.get("abandon_rate", 0) * sess)
        for pu in u.get("power_users", []):
            out["power_users"].add(pu)
        for nu in u.get("new_users", []):
            out["new_users"].add(nu)
    out["power_users_count"] = len(out["power_users"])
    out["new_users_count"] = len(out["new_users"])
    # ★2026-07-10 (世界基準評価 #6): weekly unique_users を「各日 max」で近似すると、
    #   new_users (週の和集合) が単日 max を超えて「新規17 > 週ユニーク10」の自己矛盾を吐いた。
    #   週ユニークは new/power の和集合と単日 max の**いずれも下回れない** → その最大値を下限として採る
    #   (真の週ユニークはこれ以上。日次に user_id 集合を保存していないため決定論的下限が最善)。
    single_day_max = max(
        (m.get("volume", {}).get("unique_users", 0) for m in metrics_list),
        default=0,
    )
    weekly_known = len(out["power_users"] | out["new_users"])
    out["unique_users"] = max(single_day_max, weekly_known)
    out["unique_users_is_lower_bound"] = out["unique_users"] < (
        out["new_users_count"] + out["power_users_count"]
    ) or single_day_max < weekly_known
    out.pop("power_users")
    out.pop("new_users")
    return out


# ─── 承認待ちダイジェスト (★2026-07-02 P1b) ─────────────────────────
# 3 系統の承認キュー (reflux / data_gaps / clone_feedback) が propose-only で健全に積むのに、
# 承認導線 (LINE `/reflux ok`・review dashboard) の摩擦で消費が止まり死蔵していた
# (reflux 27/27・data_gaps 175/176・clone_feedback 5/12 pending)。週次レポートに
# **決定論で** (LLM 捏造を挟まず正確な id/コマンドを) 上位を掲示し、one-tap で減らせるようにする。
ROOT = Path(__file__).resolve().parent.parent
REFLUX_QUEUE = ROOT / "data" / "brain" / "reflux_queue.jsonl"
DATA_GAPS_QUEUE = ROOT / "data" / "brain" / "clone_review" / "data_gaps.jsonl"

# data_gaps の「実ユーザー」= 社員の実質問。以下は集計/自己対話/検証由来なので除外。
_NON_EMPLOYEE_UID_PREFIXES = ("video_align", "synth", "web_chat", "test", "eval", "voice_align")


def _read_jsonl_safe(path: Path) -> list[dict]:
    if not path.exists():
        return []
    out = []
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            out.append(json.loads(line))
        except Exception:
            continue
    return out


def _is_real_employee_gap(rec: dict) -> bool:
    uid = (rec.get("user_id") or "").strip()
    if not uid:
        return False
    low = uid.lower()
    return not any(low.startswith(p) for p in _NON_EMPLOYEE_UID_PREFIXES)


PENDING_HISTORY = IMPROVE_DIR / "approval_pending_history.jsonl"


def _snapshot_pending(reflux_n: int, gaps_n: int, feedback_n) -> None:
    """★2026-07-03 ③: 承認消費の速度を測るため pending 件数を記録 (digest 生成毎)。
    ダイジェスト導入 (7/2 P1b) の前後で件数が減るかが「消費が回り始めたか」の実測になる。"""
    try:
        rec = {"ts": datetime.now(JST).isoformat(timespec="seconds"),
               "reflux_pending": reflux_n, "data_gaps_pending_real": gaps_n,
               "clone_feedback_pending": feedback_n}
        with PENDING_HISTORY.open("a", encoding="utf-8") as f:
            f.write(json.dumps(rec, ensure_ascii=False) + "\n")
    except Exception as e:
        logger.warning(f"pending snapshot failed (非致命): {e}")


def build_approval_digest(snapshot: bool = True) -> str:
    """3 承認キューの上位を決定論で整形 (id/コマンドは実データそのまま、LLM 非経由)。

    reflux は LINE `/reflux ok <id>` で one-tap 承認可。data_gaps は「取り込めば答えられる」
    社員質問の頻度上位を掲示 (該当資料を Drive/wiki へ = 解消)。clone_feedback は件数+導線。
    snapshot=False で pending 件数の計測記録を skip (dry-run の計測汚染防止、reviewer nit)。
    """
    lines = ["", "### 承認待ちダイジェスト (要アクション、自動集計)"]

    # (1) 還流 (判断軸 → Core)
    reflux = _read_jsonl_safe(REFLUX_QUEUE)
    rfx_pending = [r for r in reflux if r.get("status") == "pending"]
    rfx_pending.sort(key=lambda r: r.get("ts", ""), reverse=True)  # 新しい順
    lines.append("")
    lines.append(f"**還流 判断軸→Core — pending {len(rfx_pending)} 件、直近3:**")
    if rfx_pending:
        for r in rfx_pending[:3]:
            pr = (r.get("principle") or "").replace("\n", " ")[:44]
            dom = r.get("source_domain", "?")
            lines.append(f"- `/reflux ok {r.get('id','')}` — {pr} [{dom}]")
        lines.append("  (却下: /reflux ng <id> / 全件: /reflux)")
    else:
        lines.append("- なし")

    # (2) 社員の未解決質問 (data gaps)
    gaps = _read_jsonl_safe(DATA_GAPS_QUEUE)
    gap_pending = [r for r in gaps if r.get("status", "pending") == "pending"]
    real_gaps = [r for r in gap_pending if _is_real_employee_gap(r)]
    real_gaps.sort(key=lambda r: r.get("occurrence_count", 1), reverse=True)
    lines.append("")
    lines.append(f"**社員の未解決質問 (data gaps) — 実ユーザー pending {len(real_gaps)} 件、頻度上位5:**")
    if real_gaps:
        for r in real_gaps[:5]:
            occ = r.get("occurrence_count", 1)
            cat = r.get("matched_category", "?")
            q = (r.get("user_query") or "").replace("\n", " ")[:44]
            lines.append(f"- ×{occ} [{cat}] {q}")
        lines.append("  (= 該当資料を Drive/wiki に入れると解消。review: /api/admin/review/data-gaps 要admin)")
    else:
        lines.append("- なし")

    # (3) うみやまAI 修正希望 (clone feedback)
    fb_pending = None
    try:
        sys.path.insert(0, str(ROOT))
        import clone_feedback  # type: ignore
        for fn in ("list_pending", "get_pending", "pending"):
            if hasattr(clone_feedback, fn):
                try:
                    fb_pending = len(getattr(clone_feedback, fn)())
                    break
                except Exception:
                    continue
        if fb_pending is not None:
            lines.append("")
            lines.append(f"**うみやまAI 修正希望 (clone feedback) — pending {fb_pending} 件:** → /clone-feedback")
        # ★2026-07-11 採用レビュー #3: 👍👎 rating の週次サマリー (閉ループ = 海山が bad を見る)
        if hasattr(clone_feedback, "aggregate_ratings"):
            try:
                r = clone_feedback.aggregate_ratings(days=7)
                if r["total"] > 0:
                    lines.append("")
                    sat = round(100 * r["good"] / r["total"]) if r["total"] else 0
                    rc = "・".join(f"{k}×{v}" for k, v in
                                  sorted(r["reason_counts"].items(), key=lambda t: -t[1])) or "なし"
                    lines.append(f"**👍👎 今週の評価 — 👍{r['good']} / 👎{r['bad']} (満足{sat}%)・理由: {rc}**")
                    for b in r["recent_bad"][:3]:
                        lines.append(f"- 👎[{b['reason_label']}] 「{b['trigger']}」 → {b['response']}…")
            except Exception:
                pass
    except Exception:
        pass

    # ★2026-07-03 ③: 消費速度の実測用 snapshot (P1b 効果測定の基準線)
    if snapshot:
        _snapshot_pending(len(rfx_pending), len(real_gaps), fb_pending)

    return "\n".join(lines)


def pct_change(curr: float, prev: float) -> str:
    if not prev:
        return "—"
    r = (curr - prev) / prev * 100
    sign = "+" if r >= 0 else ""
    return f"{sign}{r:.1f}%"


def pt_change(curr: float, prev: float) -> str:
    diff = (curr - prev) * 100
    sign = "+" if diff >= 0 else ""
    return f"{sign}{diff:.1f}pt"


def _safe_div(a, b):
    return a / b if b else 0


WEEKLY_PROMPT = """あなたは「うみやまAI改善レポート」の作成者です。
過去 7 日間の自動改善ログから、海山が 3 分で読める週次レポートを書きます。
目的は「より使われるAIになる」こと。KGIは利用増・満足度向上。

# ★捏造禁止 (★2026-07-10 世界基準評価 #6、絶対遵守)
- 入力データに **無い数値・無い変化を断定しない**。「満足度が向上した」等は、入力に満足度の
  実測 (satisfaction_signals 等) が**具体値として無い限り書かない** (書くなら「計測外」と明記)。
- 質・満足度の改善は、対応する metric が入力にあり、かつ前週比が示せる時だけ「改善」と書く。
  無ければ「今週は該当指標の計測データが薄く、判断保留」と正直に書く。
- 数値は入力の値をそのまま使う。unique_users は**下限値**の場合がある (入力の
  unique_users_is_lower_bound=true の時)。その時は「X 人以上」と幅で書く。

# 入力データ

## 過去 7 日の自動改善ログ
{auto_edits}

## 過去 7 日の drafts (要レビュー)
{drafts}

## 利用指標 (今週)
{metrics_week}

## 利用指標 (前週)
{metrics_prev}

# 出力フォーマット (Markdown のみ、コードブロックなし、海山スタイル: 簡潔・冗長禁止・読了 3 分)

## うみやまAI 週次改善レポート ({week_label})

### 1 行サマリー
{{今週の一番効いた改善 or 起きた変化を 1 文で}}

### 利用指標 (前週比)

**ボリューム**
- 総会話数: {{今週件数}} (前週 {{前週件数}}, {{±X%}})
- ユニーク利用社員: {{X}} 人 (前週比 {{±N人}})
- 総ターン数: {{X}}

**深度** ← AIが"検索ツール"でなく"相談相手"になっているか
- 深いセッション (5 ターン超): {{X}} 件 ({{±X%}})
- 平均ターン数: {{X.X}}
- 早期離脱率: {{X%}} ({{±X pt}})

**質** ← AI の回答力
- 知識欠落率: {{X%}} ({{±X pt}})
- 再質問率: {{X%}} ({{±X pt}})
- 訂正率: {{X%}}

**ヘビーユーザー / 休眠ユーザー**
- パワーユーザー: {{X}} 人 (週 20 回以上利用)
- 新規利用社員: {{X}} 人
- 休眠社員: {{X}} 人

### 自動改善した内容 (確認不要)
最大 5 件、1 行ずつ。
- {{file path}} を {{create/append}} ({{根拠と期待効果を 1 行で}})

### 海山判断が必要なドラフト (要確認)
最大 10 件、優先度順。各 3 行以内。
- **{{file path}}**
  - 経緯: {{1 行}}
  - 提案: {{1 行}}
  - 判断: [ ] 承認 / [ ] 棄却 / [ ] 修正後承認

### 今週わかったこと
- 一番伸びた領域: {{XXX}} (理由仮説: {{XXX}})
- 一番落ちた領域: {{XXX}} (理由仮説: {{XXX}})
- 介入提案: {{XXX、または「なし」}}

### 来週の注目
- {{1-3 行で}}

★ 厳守:
- 海山スタイル (横文字最小、自慢しない、ドヤらない、絵文字無し)
- 数字は必ず前週比つき
- ドラフトは多くても 10 件、それ以上は次週に回す
"""


def digest_only(dry_run: bool = False) -> str:
    """★2026-07-03 ③: 承認ダイジェストだけを単発生成して push (LLM 非経由、月曜を待たない)。
    承認キューの死蔵は「週1で見る」より「気付いた時に減らせる」導線が効くため、
    週次レポート本体と独立に発射できるようにする。"""
    ensure_dirs()
    digest = build_approval_digest(snapshot=not dry_run)
    print(digest)
    if not dry_run and digest:
        line_push(f"✅ 承認待ち (押すだけで減る)\n{digest[:4000]}")
        logger.info("digest push sent")
    return digest


async def main():
    ensure_dirs()
    now = datetime.now(JST)
    week_label = (now - timedelta(days=now.weekday())).strftime("%Y-%m-%d 週")  # 月曜始まり

    end_date = now.replace(hour=0, minute=0, second=0, microsecond=0)
    edits, drafts, week_m, prev_m = load_week_data(end_date)
    logger.info(f"week edits: {len(edits)}, drafts: {len(drafts)}, metrics: {len(week_m)} days, prev: {len(prev_m)} days")

    week_agg = agg_metrics(week_m)
    prev_agg = agg_metrics(prev_m)

    # LLM に渡すデータを軽量化
    edits_compact = [
        {
            "file": e.get("file", ""),
            "operation": e.get("operation", ""),
            "impact": e.get("expected_impact", ""),
            "evidence_quotes": e.get("evidence_quotes", [])[:2],
        }
        for e in edits[-30:]  # 直近 30 件まで
    ]

    prompt = WEEKLY_PROMPT.format(
        auto_edits=json.dumps(edits_compact, ensure_ascii=False, indent=2)[:6000],
        drafts=json.dumps([{"path": d["path"], "content_preview": d["content"][:500]} for d in drafts], ensure_ascii=False, indent=2)[:6000],
        metrics_week=json.dumps(week_agg, ensure_ascii=False, indent=2, default=str),
        metrics_prev=json.dumps(prev_agg, ensure_ascii=False, indent=2, default=str),
        week_label=week_label,
    )

    try:
        # ★2026-07-10 監督者層 = Fable 5 (litellm supervisor、fallback: smart→smart-fallback)
        report = await call_llm(prompt, model=supervisor_model(), max_tokens=10000, temperature=None)
    except Exception as e:
        logger.error(f"LLM failed: {e}")
        report = f"# うみやまAI 週次改善レポート ({week_label})\n\nLLM 呼び出し失敗: {e}\n"

    # ★2026-07-10 (世界基準評価 S2): SLO/error budget を決定論で末尾に追記 (LLM に数字を語らせない)。
    try:
        from slo import build_slo_block  # noqa: E402
        report = report.rstrip() + "\n\n" + build_slo_block(days=7) + "\n"
    except Exception as e:
        logger.warning(f"SLO block failed (非致命): {e}")

    # ★2026-07-02 P1b: 承認待ちダイジェストを決定論で生成し末尾に追記 (LLM の捏造 id を排除)
    try:
        digest = build_approval_digest()
    except Exception as e:
        logger.warning(f"approval digest failed (非致命): {e}")
        digest = ""
    if digest:
        report = report.rstrip() + "\n\n" + digest + "\n"

    # 保存
    fname = now.strftime("%Y-%m-%d") + ".md"
    out_path = REPORTS_DIR / fname
    out_path.write_text(report, encoding="utf-8")
    logger.info(f"wrote {out_path}")
    print(report)

    # LINE Push (要約のみ、全文は file)
    msg_lines = report.split("\n")
    push_msg = "\n".join(msg_lines[:30])[:1800]
    push_msg += f"\n\n全文: {out_path}"
    line_push(f"📊 うみやまAI 週次レポート\n\n{push_msg}")

    # ★2026-07-02 P1b: 承認ダイジェストは「読む」でなく「押す」導線 → 別 push で全文届ける
    # (レポート本体の先頭30行 push には載らないため、one-tap コマンドが埋もれない)
    if digest:
        line_push(f"✅ 承認待ち (押すだけで減る)\n{digest[:4000]}")


if __name__ == "__main__":
    import argparse
    ap = argparse.ArgumentParser()
    ap.add_argument("--digest-only", action="store_true",
                    help="承認ダイジェストのみ単発 push (LLM 非経由、★2026-07-03 ③)")
    ap.add_argument("--dry-run", action="store_true", help="push なしで内容表示のみ")
    args = ap.parse_args()
    if args.digest_only:
        digest_only(dry_run=args.dry_run)
    elif args.dry_run:
        # ★reviewer nit: --dry-run 単独は「週次レポート全体 (LLM+push 込み) が走る」foot-gun → 拒否
        raise SystemExit("--dry-run は --digest-only とセットで使う (週次本体に dry-run は無い)")
    else:
        asyncio.run(main())
