"""Drift Detector — 各層の月次ドリフト検出 (★2026-05-21 bi-temporal 化)

役割:
1. wiki/style/, wiki/judgment/, wiki/reflex/, wiki/embodiment/ を歩く
2. 各記事の last_* タイムスタンプを見て、閾値を超えたら drift_pending マーク
3. meta/drift_log.md に「ここが古いから本人確認候補」を時系列で追記
4. audit/pending_questions.md に Q-XXX として問いを起票

bi-temporal (★追加):
- `valid_until: YYYY-MM-DD` (今日より過去) または `superseded_by: <id>` を持つ記事は
  「retired (既に置き換え済)」として drift 検出から除外する → 二重起票を防ぐ
- `superseded_by` が指す id の wiki ファイルが見つからない場合は **broken pointer** として
  audit/pending_questions.md に Q-XXX で起票 (記事は宙に浮かせない)

閾値 (schema 仕様準拠):
- style:      90 日 (last_updated)
- judgment:   90 日 (last_validated)
- reflex:     60 日 (last_observed)
- embodiment: 180 日 (last_reviewed)

実行:
- python3 /app/scripts/extractors/drift_detector.py            # 検出のみ
- python3 /app/scripts/extractors/drift_detector.py --apply    # drift_log + audit に追記
- python3 /app/scripts/extractors/drift_detector.py --apply --mark-files  # 各記事 frontmatter に validation: drift_pending を立てる
- python3 /app/scripts/extractors/drift_detector.py --retire <old_id> --replaced-by <new_id>
       # 旧記事に valid_until=today + superseded_by=new_id を立て、drift_log に置換履歴を残す
"""
from __future__ import annotations

import argparse
import logging
import re
import sys
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from _common import (  # type: ignore  # noqa: E402
    AUDIT_DIR,
    META_DIR,
    WIKI_DIR,
    parse_frontmatter,
    render_frontmatter,
    run_context,
)

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger("drift_detector")

LAYERS = {
    "style": {
        "dir": WIKI_DIR / "style",
        "field": "last_updated",
        "threshold_days": 90,
    },
    "judgment": {
        "dir": WIKI_DIR / "judgment",
        "field": "last_validated",
        "threshold_days": 90,
    },
    "reflex": {
        "dir": WIKI_DIR / "reflex",
        "field": "last_observed",
        "threshold_days": 60,
    },
    "embodiment": {
        "dir": WIKI_DIR / "embodiment",
        "field": "last_reviewed",
        "threshold_days": 180,
    },
}

DRIFT_LOG = META_DIR / "drift_log.md"
PENDING = AUDIT_DIR / "pending_questions.md"


@dataclass
class DriftFinding:
    layer: str
    file_path: Path
    field: str
    last_value: str | None
    days_old: int
    threshold: int
    file_id: str
    summary: str  # 文体: "style-vocab-001 (vocabulary, conf=high) — 92 日経過"


@dataclass
class BrokenPointer:
    """superseded_by が指す後継 id が見つからない記事。"""
    layer: str
    file_path: Path
    file_id: str
    target_id: str  # 探したけど無かった id


@dataclass
class DetectResult:
    """detect() の戻り値 (bi-temporal 化で構造化)。"""
    drift: list[DriftFinding] = field(default_factory=list)
    broken_pointers: list[BrokenPointer] = field(default_factory=list)
    retired_count: int = 0  # superseded_by or valid_until <= today で除外された数


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


def _is_retired(fm: dict, today: date) -> bool:
    """bi-temporal: 既に「終わった」記事か判定。

    - superseded_by が立っている (後継があると明示されている)
    - valid_until が今日以前 (有効期限切れ)
    どちらかが true なら retired = drift 検出から除外。
    """
    sb = (fm.get("superseded_by") or "").strip()
    if sb:
        return True
    vu = _parse_date(fm.get("valid_until"))
    if vu is not None and vu <= today:
        return True
    return False


def _layer_id_index() -> dict[str, dict[str, Path]]:
    """{layer: {file_id: path}} の index を作る。

    superseded_by の broken pointer 検出に使う。
    file_id は frontmatter.id があればそれ、無ければ stem。
    """
    idx: dict[str, dict[str, Path]] = {}
    for name, cfg in LAYERS.items():
        d: Path = cfg["dir"]
        idx[name] = {}
        if not d.exists():
            continue
        for f in d.glob("*.md"):
            if f.name.startswith("_"):
                continue
            try:
                content = f.read_text(encoding="utf-8")
            except Exception:
                continue
            fm, _ = parse_frontmatter(content)
            fid = (fm.get("id") or f.stem).strip()
            idx[name][fid] = f
            # stem でも引けるようにする (id と stem が乖離している記事用)
            if f.stem not in idx[name]:
                idx[name][f.stem] = f
    return idx


def detect() -> DetectResult:
    today = date.today()
    result = DetectResult()
    layer_idx = _layer_id_index()
    for name, cfg in LAYERS.items():
        d: Path = cfg["dir"]
        field_name: str = cfg["field"]
        thresh: int = cfg["threshold_days"]
        if not d.exists():
            continue
        for f in sorted(d.glob("*.md")):
            if f.name.startswith("_"):
                continue
            try:
                content = f.read_text(encoding="utf-8")
            except Exception:
                continue
            fm, _ = parse_frontmatter(content)
            file_id = (fm.get("id") or f.stem).strip()

            # ─── bi-temporal: retired は drift 検出からスキップ ───
            if _is_retired(fm, today):
                result.retired_count += 1
                # ただし superseded_by が指す id が存在しなければ broken pointer
                target = (fm.get("superseded_by") or "").strip()
                if target and target not in layer_idx.get(name, {}):
                    result.broken_pointers.append(
                        BrokenPointer(
                            layer=name,
                            file_path=f,
                            file_id=file_id,
                            target_id=target,
                        )
                    )
                continue

            ts = _parse_date(fm.get(field_name))
            if ts is None:
                # ない場合も drift 候補
                summary = (
                    f"{fm.get('id', f.stem)} ({name}, conf={fm.get('confidence', '?')}) "
                    f"— {field_name} 未設定"
                )
                result.drift.append(
                    DriftFinding(
                        layer=name,
                        file_path=f,
                        field=field_name,
                        last_value=None,
                        days_old=10**6,
                        threshold=thresh,
                        file_id=file_id,
                        summary=summary,
                    )
                )
                continue
            days = (today - ts).days
            if days >= thresh:
                summary = (
                    f"{fm.get('id', f.stem)} ({name}, conf={fm.get('confidence', '?')}) "
                    f"— {days} 日経過 (閾値 {thresh})"
                )
                result.drift.append(
                    DriftFinding(
                        layer=name,
                        file_path=f,
                        field=field_name,
                        last_value=ts.isoformat(),
                        days_old=days,
                        threshold=thresh,
                        file_id=file_id,
                        summary=summary,
                    )
                )
    return result


def append_drift_log(findings: list[DriftFinding]) -> int:
    if not findings:
        return 0
    today_str = date.today().isoformat()
    block = [f"\n## {today_str} drift_detector 自動検出"]
    for fnd in findings:
        try:
            rel = fnd.file_path.relative_to(WIKI_DIR.parent)
        except Exception:
            rel = fnd.file_path
        block.append("")
        block.append(f"- **対象**: `{rel}`")
        block.append(f"- **変更前**: {fnd.field}={fnd.last_value or '(未設定)'}")
        block.append(f"- **変更後**: (未変更、本人確認待ち)")
        block.append(f"- **変更理由**: 閾値 {fnd.threshold} 日経過 ({fnd.days_old} 日)")
        block.append(f"- **トリガ**: drift_detector")
        block.append(f"- **本人確認**: pending")
    text = "\n".join(block) + "\n"
    DRIFT_LOG.parent.mkdir(parents=True, exist_ok=True)
    if DRIFT_LOG.exists():
        existing = DRIFT_LOG.read_text(encoding="utf-8")
        DRIFT_LOG.write_text(existing.rstrip() + "\n" + text, encoding="utf-8")
    else:
        DRIFT_LOG.write_text(text, encoding="utf-8")
    logger.info(f"appended {len(findings)} drift entries to {DRIFT_LOG.relative_to(WIKI_DIR.parent)}")
    return len(findings)


def _next_question_index() -> int:
    if not PENDING.exists():
        return 1
    nums = [int(m.group(1)) for m in re.finditer(r"### Q-(\d{3})", PENDING.read_text(encoding="utf-8"))]
    return max(nums) + 1 if nums else 1


def append_pending_questions(findings: list[DriftFinding]) -> int:
    """drift findings をまとめて 1 件の問いとして起票する (うるさくない粒度)"""
    if not findings:
        return 0
    PENDING.parent.mkdir(parents=True, exist_ok=True)
    if not PENDING.exists():
        # ヘッダだけ作っとく
        PENDING.write_text(
            "---\ntype: audit_queue\nupdated: " + date.today().isoformat() +
            "\nclone_visibility: private\nexit_visibility: private\n---\n"
            "# Pending Questions\n\n",
            encoding="utf-8",
        )

    # layer ごとにグループ化
    by_layer: dict[str, list[DriftFinding]] = {}
    for f in findings:
        by_layer.setdefault(f.layer, []).append(f)

    blocks: list[str] = []
    base_n = _next_question_index()
    for i, (layer, items) in enumerate(sorted(by_layer.items())):
        # priority: reflex>60d は high, judgment>180d は high, ほか medium
        worst_days = max(it.days_old for it in items if it.days_old < 10**6) if items else 0
        if layer == "reflex" and worst_days >= 120:
            priority = "high"
        elif layer == "judgment" and worst_days >= 180:
            priority = "high"
        else:
            priority = "medium"
        n = base_n + i
        target_list = "\n  - ".join(f"`{it.file_id}` ({it.days_old}日)" for it in items[:20])
        more = "" if len(items) <= 20 else f"\n  - ... 他 {len(items) - 20} 件"
        block = (
            f"\n### Q-{n:03d} [{priority}] {date.today().isoformat()} (drift_detector)\n\n"
            f"- **対象**: `wiki/{layer}/` (複数)\n"
            f"  - {target_list}{more}\n"
            f"- **問い**: 上記 {layer} パターンが {LAYERS[layer]['threshold_days']} 日以上更新されていない。\n"
            f"  今もこの観察は本人像と一致しているか? (古ければ更新、消すべきなら消去判断)\n"
            f"- **なぜ**: パターンが腐ると、うみやまAI が古い自己像で応答するリスクがある。\n"
            f"- **検出元**: drift_detector\n"
            f"- **状態**: pending\n"
        )
        blocks.append(block)

    text = "\n".join(blocks)
    existing = PENDING.read_text(encoding="utf-8")
    PENDING.write_text(existing.rstrip() + "\n" + text + "\n", encoding="utf-8")
    logger.info(f"appended {len(blocks)} drift questions to {PENDING.relative_to(WIKI_DIR.parent)}")
    return len(blocks)


def mark_files(findings: list[DriftFinding]) -> int:
    """各記事 frontmatter に validation: drift_pending を立てる"""
    n = 0
    for fnd in findings:
        try:
            content = fnd.file_path.read_text(encoding="utf-8")
            fm, body = parse_frontmatter(content)
            if fm.get("validation") == "drift_pending":
                continue
            fm["validation"] = "drift_pending"
            new_content = render_frontmatter(fm) + body
            fnd.file_path.write_text(new_content, encoding="utf-8")
            n += 1
        except Exception as e:
            logger.warning(f"failed to mark {fnd.file_path}: {e}")
    if n:
        logger.info(f"marked {n} files as validation: drift_pending")
    return n


def append_broken_pointer_questions(broken: list[BrokenPointer]) -> int:
    """broken pointer (superseded_by が存在しない id を指している) を pending に起票。

    drift 起票と分けて 1 件ずつ起票する (broken は high priority 個別事案のため)。
    既存 pending に同じ署名 (layer + file_id + target_id) があれば追記しない (dedup)。
    """
    if not broken:
        return 0
    PENDING.parent.mkdir(parents=True, exist_ok=True)
    if not PENDING.exists():
        PENDING.write_text(
            "---\ntype: audit_queue\nupdated: " + date.today().isoformat() +
            "\nclone_visibility: private\nexit_visibility: private\n---\n"
            "# Pending Questions\n\n",
            encoding="utf-8",
        )

    existing_text = PENDING.read_text(encoding="utf-8")
    blocks: list[str] = []
    base_n = _next_question_index()
    added = 0
    for bp in broken:
        sig = f"broken_pointer:{bp.layer}:{bp.file_id}:{bp.target_id}"
        # dedup: 同じ署名の問いが既存にあれば skip
        if sig in existing_text:
            continue
        n = base_n + added
        block = (
            f"\n### Q-{n:03d} [high] {date.today().isoformat()} (drift_detector)\n\n"
            f"- **対象**: `wiki/{bp.layer}/{bp.file_path.name}` (id=`{bp.file_id}`)\n"
            f"- **問い**: superseded_by が `{bp.target_id}` を指しているが、その後継記事が "
            f"`wiki/{bp.layer}/` に見つからない。\n"
            f"  (a) 後継 id をタイポ修正 / (b) 後継記事を新規作成 / "
            f"(c) superseded_by を外す のいずれか。\n"
            f"- **なぜ**: bi-temporal の broken pointer を放置すると、本人像の遷移が追えなくなる。\n"
            f"- **検出元**: drift_detector (broken_pointer)\n"
            f"- **署名**: {sig}\n"
            f"- **状態**: pending\n"
        )
        blocks.append(block)
        added += 1

    if not blocks:
        return 0
    text = "\n".join(blocks)
    PENDING.write_text(existing_text.rstrip() + "\n" + text + "\n", encoding="utf-8")
    logger.info(f"appended {len(blocks)} broken_pointer questions to {PENDING.relative_to(WIKI_DIR.parent)}")
    return len(blocks)


def retire_one(old_id: str, replaced_by: str | None) -> int:
    """1 件の記事を retired にマーク (valid_until=today + superseded_by=<new_id>)。

    layer は wiki/style, wiki/judgment, wiki/reflex, wiki/embodiment を横断検索。
    複数 layer に同一 id があった場合は全部にマーク (普通は起きないが安全側)。
    drift_log にも置換イベントを残す。

    返り値: マークしたファイル数 (0 = 見つからなかった)。
    """
    today = date.today()
    idx = _layer_id_index()
    matched: list[tuple[str, Path]] = []
    for layer, m in idx.items():
        if old_id in m:
            matched.append((layer, m[old_id]))

    if not matched:
        logger.warning(f"retire: id={old_id} not found in any layer")
        return 0

    drift_block: list[str] = []
    n = 0
    for layer, path in matched:
        try:
            content = path.read_text(encoding="utf-8")
        except Exception as e:
            logger.warning(f"retire: failed to read {path}: {e}")
            continue
        fm, body = parse_frontmatter(content)
        if not fm.get("valid_until"):
            fm["valid_until"] = today.isoformat()
        if replaced_by:
            fm["superseded_by"] = replaced_by
        new_content = render_frontmatter(fm) + body
        try:
            path.write_text(new_content, encoding="utf-8")
            n += 1
            rel = path.relative_to(WIKI_DIR.parent)
            drift_block.append(f"- **対象**: `{rel}` (id=`{old_id}`)")
            drift_block.append(f"- **変更前**: 現役")
            drift_block.append(
                f"- **変更後**: retired (valid_until={today.isoformat()}"
                + (f", superseded_by={replaced_by}" if replaced_by else "") + ")"
            )
            drift_block.append(f"- **変更理由**: 手動 retire (drift_detector --retire)")
            drift_block.append(f"- **トリガ**: drift_detector --retire")
            drift_block.append(f"- **本人確認**: 完了 (海山指示で実行)")
            drift_block.append("")
        except Exception as e:
            logger.warning(f"retire: failed to write {path}: {e}")

    if drift_block:
        header = f"\n## {today.isoformat()} drift_detector --retire ({old_id})\n\n"
        text = header + "\n".join(drift_block) + "\n"
        DRIFT_LOG.parent.mkdir(parents=True, exist_ok=True)
        if DRIFT_LOG.exists():
            existing = DRIFT_LOG.read_text(encoding="utf-8")
            DRIFT_LOG.write_text(existing.rstrip() + "\n" + text, encoding="utf-8")
        else:
            DRIFT_LOG.write_text(text, encoding="utf-8")
        logger.info(f"retired {n} file(s) for id={old_id}, drift_log updated")
    return n


def main() -> None:
    ap = argparse.ArgumentParser(description="Drift detector for self-replication wiki")
    ap.add_argument("--apply", action="store_true", help="drift_log と pending_questions に追記")
    ap.add_argument("--mark-files", action="store_true", help="各記事 frontmatter に validation: drift_pending")
    ap.add_argument("--quiet", action="store_true")
    ap.add_argument("--retire", metavar="OLD_ID",
                    help="bi-temporal: 旧記事 (style/judgment/reflex/embodiment 横断) を retired にマーク "
                         "(valid_until=today + superseded_by=NEW_ID)")
    ap.add_argument("--replaced-by", metavar="NEW_ID",
                    help="--retire と組で。後継記事の id。省略時は valid_until のみ立てる (= 単純廃止)。")
    args = ap.parse_args()

    if args.quiet:
        logger.setLevel(logging.WARNING)

    # ─── --retire mode (bi-temporal manual retirement) ───
    if args.retire:
        with run_context(
            "drift_detector",
            mode="retire",
            old_id=args.retire,
            replaced_by=args.replaced_by,
        ) as ctx:
            n = retire_one(args.retire, args.replaced_by)
            ctx["files_retired"] = n
            ctx["status"] = "retired" if n else "not_found"
        return

    with run_context(
        "drift_detector",
        apply=args.apply,
        mark_files=args.mark_files,
    ) as ctx:
        result = detect()

        ctx["findings"] = len(result.drift)
        ctx["broken_pointers"] = len(result.broken_pointers)
        ctx["retired_skipped"] = result.retired_count
        ctx["by_layer"] = {
            layer: sum(1 for f in result.drift if f.layer == layer) for layer in LAYERS.keys()
        }

        if result.retired_count:
            logger.info(
                f"skipped {result.retired_count} retired file(s) "
                f"(superseded_by or valid_until <= today)"
            )

        if not result.drift and not result.broken_pointers:
            logger.info("no drift detected. all up to date.")
            ctx["status"] = "clean"
            return

        if result.drift:
            logger.info(f"{len(result.drift)} drift findings:")
            for fnd in result.drift[:30]:
                logger.info(f"  - {fnd.summary}")
            if len(result.drift) > 30:
                logger.info(f"  ... and {len(result.drift) - 30} more")

        if result.broken_pointers:
            logger.warning(f"{len(result.broken_pointers)} broken pointer(s):")
            for bp in result.broken_pointers[:20]:
                logger.warning(
                    f"  - {bp.layer}/{bp.file_path.name}: "
                    f"superseded_by={bp.target_id} 不在"
                )

        if args.apply:
            ctx["drift_log_appended"] = append_drift_log(result.drift)
            ctx["pending_appended"] = append_pending_questions(result.drift)
            ctx["broken_pointer_appended"] = append_broken_pointer_questions(result.broken_pointers)
        if args.mark_files:
            ctx["files_marked"] = mark_files(result.drift)

        if not args.apply and not args.mark_files:
            logger.info("(detection only — re-run with --apply to persist)")
            ctx["status"] = "detect_only"
        else:
            ctx["status"] = "applied"


if __name__ == "__main__":
    main()
