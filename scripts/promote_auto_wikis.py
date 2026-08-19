#!/usr/bin/env python3
"""scripts/promote_auto_wikis.py — 自動生成 wiki の可視性を明示化する (★2026-08-06 海山指示「public に昇格」)

背景: clone_auto_improve が生成した wiki 62 件は frontmatter を持たず、visibility の
fail-safe で **private** に落ちていた。= 社員クローンから永久に読めず、knowledge_gap の
自動修正アームが「書いた瞬間に見えない場所へ落ちる」状態だった (実測 62/62)。

本 script は「frontmatter が無いから偶然 private」を「意図して public / private」に変える。
分類は海山の指示と §1.15 cross-check の監査結果に基づく固定リストで、推測はしない
(自動判定にすると、まさに今回事故を起こした『AI が中身を見ずに決める』構図の再生産になる)。

usage:
  python3 scripts/promote_auto_wikis.py --dry-run   # 差分だけ表示
  python3 scripts/promote_auto_wikis.py --apply
  python3 scripts/promote_auto_wikis.py --rollback  # 退避から復元
"""
from __future__ import annotations

import argparse
import shutil
import sys
from datetime import date
from pathlib import Path

BASE = Path(__file__).resolve().parents[1]
WIKI = BASE / "data" / "brain" / "wiki"
BACKUP = BASE / "data" / "brain" / "wiki_promotion_backup" / "2026-08-06"

# ─── 分類 (海山 2026-08-06 判断 + cross-check 監査) ────────────────────────
# public: 社員が業務で使う知識。うみやまAI の retrieval に載る
PUBLIC = [
    "2024年度トピック.md", "cockpit-dashboard-usage-rate.md", "competitive_strategy.md",
    "contact-ec-improvement-priorities.md", "contact-lens-store-policy.md", "create-link.md",
    "culture-promotion-kpi.md", "culture-promotion-strategy.md",
    "factory-communication.md", "fixture_management.md",
    "giant_killing_strategy.md", "how-to-share-data-with-umiyama-ai.md",
    "inner_branding_phases.md", "inventory_management.md",     "lenskart-tango-nexus-rfid.md", "line-works-bot-setup.md", "ma-tool-comparison.md",
    "mermaid-compatibility.md",     "okinawa-sales-performance.md",
    "organizational_responsibility.md", "own_days_branding_shelves.md",
    "owndays-digital-strategy.md",     "point-grant-form-batch-system.md", "pricing_strategy.md", "product-department-challenges.md",
    "rfid-operations.md", "rfid-tag-evaluation.md", "sales-visualization.md",
    "shinjuku-east-rent.md", "shipping-options.md",     "store-renovation-impact.md",     "vmv-kgi-design.md", "warehouse-heat-safety.md",
    ]

# private: 出さない。理由を frontmatter に残し「なぜ private か」を後任が読めるようにする
PRIVATE: dict[str, str] = {
    # 実名の個人ファイル (§1.9 PII)
    "people/garvit-garg.md": "個人ファイル (実名)",
    "people/ogita-maho.md": "個人ファイル (実名)",
    "people/sogihara-hiroshi.md": "個人ファイル (実名・空)",
    "people/海山丈司.md": "個人ファイル (実名)",
    "analysis/lineworks-アルフレッドアダムンフォー-2026-08-04.md": "実名の個人分析 (§1.9 PII)",
    "analysis/lineworks-平林真之-2026-08-05.md": "実名の個人分析 (§1.9 PII)",
    # 機密 (海山 2026-08-06 判断: 財務数値と出店候補は社員に開示しない)
    "knowledge/owndays-company-scale-2026.md": "未開示の財務数値 (FY26 EBITDA/AOP/FY27目標)",
    "knowledge/owndays-whitespace.md": "出店候補地の具体名",
    "decisions/2026-06-12-laos-wholesale-currency-change.md": "取引条件 (卸売価格の通貨変更・契約) + 実名",
    "decisions/2026-06-12-product-center-final-bom.md": "取引先の BOM・仕入情報",
    "decisions/2026-06-12-sales-analysis.md": "国別 SKU 分析 (未公開)",
    "decisions/2026-06-12-sg-conversion-meeting-reschedule.md": "社内調整の経緯 + 実名",
}

# ★2026-08-13 再ローンチ総点検で処理済み (本番 wiki を直接更新、data/ は git 非追跡):
#   - retired (superseded_by 付与、canonical と競合していた): kansai_sales /
#     tokyo-performance-analysis / owndays-sales-data / sales-top10 /
#     maternity_leave_compensation (正本 = 育児介護休業規程 + 給与規程が索引済)
#   - 修復して public 復帰: national_qualifications (実数 24 名/1級 7 名/FY27 目標 50 名を
#     本部会議資料から転記、10 月要更新)、side_jobs_policy (AI 創作の申請フローを除去し
#     就業規則 第33条(10) 準拠に書き換え)
# 昇格前に内容の修復が要るもの (誤データ / 空箱 / 能力否定のみ)
NEEDS_FIX: dict[str, str] = {
    "knowledge/owndays-recent-decisions.md": "§1.15 監査で差し止め — 未発表の人事・報酬制度変更 (等級統一/インセンティブ再設計/マイレージ料率) + NDA 締結進行中のウェアラブル案件",
    "knowledge/ec-lens-exchange-campaign-issues.md": "§1.15 監査で差し止め — 過重労働・健康被害の記録。privacy_review が同内容を『健康深刻情報』で archive 済 (§1.9)。少人数部署で個人特定可",
    "knowledge/office-environment-consultation.md": "§1.15 監査で差し止め — §1.9 (k) 相談系。実在の個別相談から生成され、相談者・対象者が同僚には推測可能",
    "knowledge/tattoo-policy.md": "§1.15 監査で差し止め — モデル起用基準だが『タトゥーは大丈夫か』への身だしなみ規定として返る恐れ。外見規定は人権配慮領域",
    "knowledge/資格運用.md": "§1.15 監査で差し止め — 制度未確定と自認しながら受験料補助の可否に踏み込む。『AI が通ると言った』の根拠にされる",
    "knowledge/stapa_shachomeshi.md": "§1.15 監査で差し止め — 海山自身の施策の終了理由を根拠ゼロの推測で断定。CEO の声で出る",
    "knowledge/owndays-area-top-stores.md": "「記録する予定」だけの空箱",
    "knowledge/owndays-store-history.md": "「記録する予定」だけの空箱",
    "knowledge/employee_count_japan.md": "「公開されていません」のみ = 能力否定 (2026-08-06 事故と同型)",
    "knowledge/cvr_data_availability.md": "「現在公開されていません」のみ = 能力否定",
}


def _frontmatter(vis: str, note: str = "") -> str:
    lines = [
        "---",
        f"updated: {date.today().isoformat()}",
        "confidence: medium",
        f"clone_visibility: {vis}",
        "source: clone_auto_improve (自動生成)",
        "reviewed: 2026-08-06 海山指示で可視性を明示化",
    ]
    if note:
        lines.append(f"visibility_reason: {note}")
    lines.append("---")
    return "\n".join(lines) + "\n"


def plan() -> list[tuple[Path, str, str]]:
    """(path, visibility, note) の一覧。frontmatter が既に在るものは触らない。"""
    out: list[tuple[Path, str, str]] = []
    for rel in PUBLIC:
        out.append((WIKI / "knowledge" / rel, "public", ""))
    for rel, note in PRIVATE.items():
        out.append((WIKI / rel, "private", note))
    for rel, note in NEEDS_FIX.items():
        out.append((WIKI / rel, "private", f"要修復のため保留 — {note}"))
    return out


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--apply", action="store_true")
    ap.add_argument("--rollback", action="store_true")
    args = ap.parse_args()

    if args.rollback:
        if not BACKUP.exists():
            print(f"退避が無い: {BACKUP}")
            return 1
        n = 0
        for src in BACKUP.rglob("*.md"):
            dst = WIKI / src.relative_to(BACKUP)
            if dst.exists():
                shutil.copy2(src, dst)
                n += 1
        print(f"復元 {n} 件 (frontmatter 無しの元状態に戻した)")
        return 0

    counts = {"public": 0, "private": 0, "skip": 0, "missing": 0}
    for path, vis, note in plan():
        if not path.exists():
            print(f"  [欠落] {path.relative_to(WIKI)}")
            counts["missing"] += 1
            continue
        body = path.read_text(encoding="utf-8")
        if body.lstrip().startswith("---"):
            counts["skip"] += 1  # 既に明示済み = 触らない
            continue
        if args.apply:
            path.write_text(_frontmatter(vis, note) + body.lstrip("\n"), encoding="utf-8")
        counts[vis] += 1
    verb = "適用" if args.apply else "dry-run"
    print(f"{verb}: public {counts['public']} / private {counts['private']} "
          f"/ 既に明示済み {counts['skip']} / 欠落 {counts['missing']}")
    if not args.apply:
        print("  → --apply で書き込み。--rollback で退避から復元")
    return 0


if __name__ == "__main__":
    sys.exit(main())
