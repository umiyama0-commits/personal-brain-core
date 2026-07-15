"""Audit Generator — 確認すべき問いの自動生成

drift_detector が「時間軸の腐り」を検出するのに対し、
本スクリプトは **構造軸の不整合** を検出して audit/pending_questions.md に起票する。

検出する不整合:
1. schema_violation:
   - style: confidence=high なのに evidence < 5 件
   - judgment: confidence=high なのに evidence < 5 件
   - reflex: confidence=high なのに evidence < 5 件
   - embodiment: training_eligible=yes なのに training_eligible_reason 空
2. broken_link:
   - judgment.evidence が wiki/decisions/ や raw/ に存在しないパスを指している
3. identity_stale:
   - wiki/identity.md に last_validated が無い、または > 60 日経過
4. empty_layer:
   - raw/ が豊富にあるのに wiki/<layer>/ が 0 件
5. counter_evidence_zero:
   - confidence=high なのに counter_evidence が完全に空 (本人が反証を検討してない疑い)

実行:
- python3 /app/scripts/extractors/audit_generator.py            # 検出のみ
- python3 /app/scripts/extractors/audit_generator.py --apply    # pending_questions.md に追記
"""
from __future__ import annotations

import argparse
import logging
import re
import sys
from dataclasses import dataclass, field
from datetime import date, datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from _common import (  # type: ignore  # noqa: E402
    APP_ROOT,
    AUDIT_DIR,
    DATA_BRAIN,
    RAW_DIR,
    WIKI_DIR,
    parse_frontmatter,
    run_context,
)

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger("audit_generator")

PENDING = AUDIT_DIR / "pending_questions.md"


@dataclass
class AuditFinding:
    kind: str  # schema_violation | broken_link | identity_stale | empty_layer | counter_evidence_zero
    file_path: Path | None
    detail: str
    priority: str = "medium"  # low | medium | high


def _parse_date(s: str | None) -> date | None:
    if not s:
        return None
    s = s.strip().strip('"').strip("'")
    for fmt in ("%Y-%m-%d", "%Y/%m/%d", "%Y.%m.%d"):
        try:
            return datetime.strptime(s, fmt).date()
        except Exception:
            pass
    return None


def _ev_count(fm: dict) -> int:
    ev = fm.get("evidence")
    if isinstance(ev, list):
        return len(ev)
    if isinstance(ev, str) and ev.strip():
        return 1
    return 0


def _counter_count(fm: dict) -> int:
    ce = fm.get("counter_evidence")
    if isinstance(ce, list):
        return len(ce)
    if isinstance(ce, str) and ce.strip():
        return 1
    return 0


def _check_schema_violation(layer_dir: Path, layer: str) -> list[AuditFinding]:
    findings: list[AuditFinding] = []
    if not layer_dir.exists():
        return findings
    for f in sorted(layer_dir.glob("*.md")):
        if f.name.startswith("_"):
            continue
        try:
            content = f.read_text(encoding="utf-8")
        except Exception:
            continue
        fm, _ = parse_frontmatter(content)
        conf = fm.get("confidence")
        ev = _ev_count(fm)
        if layer in ("style", "judgment", "reflex"):
            if conf == "high" and ev < 5:
                findings.append(AuditFinding(
                    kind="schema_violation",
                    file_path=f,
                    detail=f"{layer}: confidence=high なのに evidence={ev} (5 必要)",
                    priority="medium",
                ))
            if conf == "high" and _counter_count(fm) == 0:
                findings.append(AuditFinding(
                    kind="counter_evidence_zero",
                    file_path=f,
                    detail=f"{layer}: confidence=high なのに counter_evidence 0 件 (反証検討の痕跡なし)",
                    priority="low",
                ))
        if layer == "embodiment":
            if fm.get("training_eligible") == "yes" and not fm.get("training_eligible_reason"):
                findings.append(AuditFinding(
                    kind="schema_violation",
                    file_path=f,
                    detail="embodiment: training_eligible=yes なのに reason 空",
                    priority="high",
                ))
        # 可視性整合性: clone_visibility=private && exit_visibility=public は意味矛盾
        cv = fm.get("clone_visibility")
        ev_vis = fm.get("exit_visibility")
        if cv == "private" and ev_vis == "public":
            findings.append(AuditFinding(
                kind="visibility_inconsistency",
                file_path=f,
                detail=f"{layer}: clone_visibility=private なのに exit_visibility=public (内部 AI が踏まないのに公開出口は意味矛盾)",
                priority="medium",
            ))
    return findings


def _check_broken_links(layer_dir: Path) -> list[AuditFinding]:
    """judgment.evidence などが指す path が存在するか確認"""
    findings: list[AuditFinding] = []
    if not layer_dir.exists():
        return findings
    for f in sorted(layer_dir.glob("*.md")):
        if f.name.startswith("_"):
            continue
        try:
            content = f.read_text(encoding="utf-8")
        except Exception:
            continue
        fm, _ = parse_frontmatter(content)
        ev = fm.get("evidence")
        if not isinstance(ev, list):
            continue
        broken = []
        for e in ev:
            if not isinstance(e, str) or not e.strip():
                continue
            # 相対 path の解釈: data/brain/ 起点 (raw/, wiki/) または app/ 起点
            cand = [
                DATA_BRAIN / e,
                APP_ROOT / e,
                Path(e),
            ]
            if not any(p.exists() for p in cand):
                broken.append(e)
        if broken:
            findings.append(AuditFinding(
                kind="broken_link",
                file_path=f,
                detail=f"evidence path 不在: {broken[:5]}" + (" ..." if len(broken) > 5 else ""),
                priority="medium",
            ))
    return findings


def _check_identity() -> list[AuditFinding]:
    findings: list[AuditFinding] = []
    p = WIKI_DIR / "identity.md"
    if not p.exists():
        return findings
    try:
        content = p.read_text(encoding="utf-8")
    except Exception:
        return findings
    fm, _ = parse_frontmatter(content)
    lv = _parse_date(fm.get("last_validated"))
    if lv is None:
        findings.append(AuditFinding(
            kind="identity_stale",
            file_path=p,
            detail="identity.md に last_validated が無い (Q-005 と同根)",
            priority="high",
        ))
    else:
        days = (date.today() - lv).days
        if days > 60:
            findings.append(AuditFinding(
                kind="identity_stale",
                file_path=p,
                detail=f"identity.md last_validated から {days} 日経過 (60 日閾値)",
                priority="high",
            ))
    return findings


def _check_empty_layers() -> list[AuditFinding]:
    """raw が豊富なのに wiki/<layer>/ がほぼ空"""
    findings: list[AuditFinding] = []
    raw_n = 0
    if (RAW_DIR / "conversations").exists():
        raw_n += sum(1 for _ in (RAW_DIR / "conversations").glob("*.md"))
    if (RAW_DIR / "notes").exists():
        raw_n += sum(1 for _ in (RAW_DIR / "notes").glob("*.md"))
    if raw_n < 30:
        return findings  # raw 自体が少ない時は警告しない

    for layer in ("style", "judgment", "reflex"):
        d = WIKI_DIR / layer
        if not d.exists():
            continue
        n = sum(1 for f in d.glob("*.md") if not f.name.startswith("_"))
        if n == 0:
            findings.append(AuditFinding(
                kind="empty_layer",
                file_path=None,
                detail=f"wiki/{layer}/ が空 (raw {raw_n} 件あり)。{layer}_extractor を実行すべき。",
                priority="medium",
            ))
    return findings


def detect_all() -> list[AuditFinding]:
    out: list[AuditFinding] = []
    for layer in ("style", "judgment", "reflex", "embodiment"):
        out += _check_schema_violation(WIKI_DIR / layer, layer)
    out += _check_broken_links(WIKI_DIR / "judgment")
    out += _check_broken_links(WIKI_DIR / "style")
    out += _check_broken_links(WIKI_DIR / "reflex")
    out += _check_identity()
    out += _check_empty_layers()
    return out


def _next_question_index() -> int:
    if not PENDING.exists():
        return 1
    nums = [int(m.group(1)) for m in re.finditer(r"### Q-(\d{3})", PENDING.read_text(encoding="utf-8"))]
    return max(nums) + 1 if nums else 1


def _existing_pending_signatures() -> set[str]:
    """既に pending_questions.md に存在する未解決の問いの "署名" を抽出。

    署名 = (kind, 対象詳細の最初の 60 字) のハッシュ的代表。
    重複追記防止に使う。
    state: resolved / done で始まるブロックは "解決済み" 扱いで除外
    (= 同じ kind が再発したら新しく Q を立てる)。
    """
    if not PENDING.exists():
        return set()
    text = PENDING.read_text(encoding="utf-8")
    sigs: set[str] = set()
    # ### Q-NNN [...] (audit_generator: <kind>) ... - **対象**: <details> ... - **状態**: pending
    pattern = re.compile(
        r"### Q-\d+ \[[a-z]+\] [\d\-]+ \(audit_generator: ([^)]+)\)\n\n"
        r"- \*\*対象\*\*:\n  - (.+?)\n.*?\*\*状態\*\*: (\w+)",
        re.DOTALL,
    )
    for m in pattern.finditer(text):
        kind, detail, state = m.group(1).strip(), m.group(2).strip(), m.group(3).strip()
        if state in {"resolved", "done", "fixed"}:
            continue
        # 詳細の先頭 80 字を署名にする (細かい数値変動は許容しない方が良い)
        sig = f"{kind}::{detail[:80]}"
        sigs.add(sig)
    return sigs


def _finding_signature(kind: str, items: list[AuditFinding]) -> str:
    """append 直前の findings 側の署名 (上記と対応)。"""
    first_detail = (
        f"`{items[0].file_path.relative_to(WIKI_DIR.parent)}`: {items[0].detail}"
        if items[0].file_path
        else items[0].detail
    )
    return f"{kind}::{first_detail[:80]}"


def append_pending(findings: list[AuditFinding]) -> int:
    if not findings:
        return 0
    PENDING.parent.mkdir(parents=True, exist_ok=True)
    if not PENDING.exists():
        PENDING.write_text(
            "---\ntype: audit_queue\nupdated: " + date.today().isoformat() +
            "\nclone_visibility: private\nexit_visibility: private\n---\n"
            "# Pending Questions\n\n",
            encoding="utf-8",
        )
    base = _next_question_index()
    blocks: list[str] = []
    today = date.today().isoformat()

    # kind ごとにグループ化
    by_kind: dict[str, list[AuditFinding]] = {}
    for fnd in findings:
        by_kind.setdefault(fnd.kind, []).append(fnd)

    # 既存 pending を読んで重複追記を防ぐ
    existing_sigs = _existing_pending_signatures()
    duplicates_skipped = 0

    next_n = base
    for kind, items in sorted(by_kind.items()):
        # 重複検出: 既に pending な同等問いがあればスキップ
        sig = _finding_signature(kind, items)
        if sig in existing_sigs:
            duplicates_skipped += 1
            logger.info(f"skip duplicate pending question (kind={kind}): already in pending")
            continue

        priority = max(items, key=lambda x: {"low": 0, "medium": 1, "high": 2}[x.priority]).priority
        details = "\n  - ".join(
            (f"`{it.file_path.relative_to(WIKI_DIR.parent)}`: {it.detail}" if it.file_path else it.detail)
            for it in items[:15]
        )
        more = "" if len(items) <= 15 else f"\n  - ... 他 {len(items) - 15} 件"
        question_map = {
            "schema_violation": "スキーマ違反: 上記記事は confidence と evidence/フィールド整合性が崩れている。confidence を下げるか、evidence を補完するか?",
            "broken_link": "壊れた参照: evidence path が存在しない記事がある。リンク修正 or 該当 raw 復元が必要。",
            "identity_stale": "identity.md の継続検証: 価値観・人物像の中核は最も腐りやすい。今もこの記述で本人と一致するか?",
            "empty_layer": "層の空状態: raw は十分にあるのに該当層が空。抽出器を実行すべきタイミング。",
            "counter_evidence_zero": "反証ゼロ: confidence=high なのに counter_evidence が空。本人が一度も反証を検討していない可能性。",
            "visibility_inconsistency": "可視性矛盾: clone_visibility=private (内部 AI 不参照) なのに exit_visibility=public (出口公開可) は意味的に矛盾。どちらかを訂正すべき。",
        }
        block = (
            f"\n### Q-{next_n:03d} [{priority}] {today} (audit_generator: {kind})\n\n"
            f"- **対象**:\n  - {details}{more}\n"
            f"- **問い**: {question_map.get(kind, '上記の不整合を確認したい。')}\n"
            f"- **なぜ**: 構造軸の不整合は、ドリフトとは別の経路で wiki の信頼性を蝕む。\n"
            f"- **検出元**: audit_generator\n"
            f"- **状態**: pending\n"
        )
        blocks.append(block)
        next_n += 1

    if duplicates_skipped:
        logger.info(f"{duplicates_skipped} duplicate audit findings skipped (already in pending).")

    if not blocks:
        return 0

    text = "\n".join(blocks)
    existing = PENDING.read_text(encoding="utf-8")
    PENDING.write_text(existing.rstrip() + "\n" + text + "\n", encoding="utf-8")
    logger.info(f"appended {len(blocks)} audit questions to {PENDING.relative_to(WIKI_DIR.parent)}")
    return len(blocks)


def main() -> None:
    ap = argparse.ArgumentParser(description="Audit question generator (structural inconsistency)")
    ap.add_argument("--apply", action="store_true", help="pending_questions.md に追記")
    ap.add_argument("--quiet", action="store_true")
    args = ap.parse_args()

    with run_context("audit_generator", apply=args.apply) as ctx:
        findings = detect_all()
        if args.quiet:
            logger.setLevel(logging.WARNING)

        ctx["findings"] = len(findings)
        # kind 別集計
        by_kind: dict[str, int] = {}
        for fnd in findings:
            by_kind[fnd.kind] = by_kind.get(fnd.kind, 0) + 1
        ctx["by_kind"] = by_kind

        if not findings:
            logger.info("no audit findings. structure is consistent.")
            ctx["status"] = "clean"
            return

        logger.info(f"{len(findings)} audit findings:")
        for fnd in findings[:30]:
            loc = fnd.file_path.relative_to(WIKI_DIR.parent) if fnd.file_path else "(global)"
            logger.info(f"  - [{fnd.priority}] {fnd.kind}: {loc}: {fnd.detail}")
        if len(findings) > 30:
            logger.info(f"  ... and {len(findings) - 30} more")

        if args.apply:
            ctx["pending_appended"] = append_pending(findings)
            ctx["status"] = "applied"
        else:
            logger.info("(detection only — re-run with --apply to persist)")
            ctx["status"] = "detect_only"


if __name__ == "__main__":
    main()
