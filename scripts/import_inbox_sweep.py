#!/usr/bin/env python3
"""scripts/import_inbox_sweep.py — git 経由で届く chat export の配送 (★2026-07-05 海山指示「wikiに」)

背景: リモート session (Claude Code on the web) からは Mac mini の IMPORT_DIR
(data/brain/import = git 非管理) に直接ファイルを置けない。git 追跡の
**data/brain/import_inbox/** を「配送用 inbox」とし、本 script (cron、15分毎) が
manifest.json のドメイン宣言に従って routing する:

  - domain "owndays" (既定):  IMPORT_DIR へ copy → 既存 _watch_import_dir が
                              chat_import parse → PrivacyGate → LLM compile → wiki
                              (従来 pipeline そのまま、経路追加なし)
  - domain "personal/<pj>":   **IMPORT_DIR 非経由** (§1.17 = personal は OWNDAYS compile に
                              渡さない)。chat_import で決定論 parse → wiki/personal/<pj>/imports/
                              に private transcript md を直接書く (LLM 不使用、
                              gdrive_sync の personal 直書き・claude_personal_sync と同思想)

冪等: data/brain/.import_inbox_state.json に sha256 を記録 (git 再 pull / 再実行で二重配送しない)。
inbox のファイルは git の提出アーカイブとして残す (削除しない = auto_deploy の pull と喧嘩しない)。
§1.18 loud_fail 配線 (配送 = 取込系統。連続失敗で LINE 通知)。

manifest.json 形式:
  {"files": {"<filename>": {"domain": "owndays" | "personal/<pj>", "label": "表示名"}}}
manifest に無い .txt は owndays 扱い (安全側 = PrivacyGate を必ず通る)。

実行:
  python3 scripts/import_inbox_sweep.py --dry-run   # 配送予定の確認
  python3 scripts/import_inbox_sweep.py             # 配送
"""
from __future__ import annotations

import argparse
import hashlib
import json
import logging
import os
import shutil
import sys
from datetime import date
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "scripts"))

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger("import_inbox_sweep")

BRAIN_ROOT = Path(os.getenv("BRAIN_ROOT", ROOT / "data" / "brain"))
INBOX = BRAIN_ROOT / "import_inbox"
IMPORT_DIR = BRAIN_ROOT / "import"
WIKI_DIR = BRAIN_ROOT / "wiki"
STATE_FILE = BRAIN_ROOT / ".import_inbox_state.json"


def _sha(p: Path) -> str:
    return hashlib.sha256(p.read_bytes()).hexdigest()


def _load_state() -> dict:
    if STATE_FILE.exists():
        try:
            return json.loads(STATE_FILE.read_text(encoding="utf-8"))
        except Exception:
            pass
    return {"delivered": {}}


def _save_state(st: dict) -> None:
    STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
    STATE_FILE.write_text(json.dumps(st, ensure_ascii=False, indent=2), encoding="utf-8")


def _load_manifest() -> dict:
    mf = INBOX / "manifest.json"
    if mf.exists():
        try:
            return json.loads(mf.read_text(encoding="utf-8")).get("files", {})
        except Exception as e:
            logger.warning(f"manifest.json 読込失敗 (全件 owndays 扱い): {e}")
    return {}


def _render_personal_transcript(src: Path, label: str, project: str) -> str:
    """chat export を決定論 parse して private transcript md を組み立てる (LLM 不使用)。"""
    from chat_import import detect_chat_format, parse_line_export, parse_whatsapp_export
    from services.life_story import sanitize_chapter

    text = src.read_text(encoding="utf-8", errors="replace")
    fmt = detect_chat_format(text)
    if fmt == "line":
        msgs = parse_line_export(src)
    elif fmt == "whatsapp":
        msgs = parse_whatsapp_export(src)
    else:
        raise ValueError(f"chat 形式を判定できない: {src.name}")
    if not msgs:
        raise ValueError(f"メッセージ 0 件: {src.name}")

    lines: list[str] = []
    cur_date = None
    for m in msgs:
        if m["date"] != cur_date:
            cur_date = m["date"]
            lines.append(f"\n## {cur_date or '(日付不明)'}\n")
        body = m["text"].replace("\n", "\n  ")   # 複数行は bullet 継続 indent
        lines.append(f"- {m['time']} {m['sender']}: {body}")

    src_label = "LINE" if fmt == "line" else "WhatsApp"
    header = (
        "---\n"
        "clone_visibility: private\n"
        "exit_visibility: private\n"
        "domain: personal\n"
        f"project: {project}\n"
        f"source: {src_label.lower()}-export\n"
        f"imported: {date.today().isoformat()}\n"
        "---\n"
        f"# {src_label} トーク: {label}\n\n"
        f"(chat export の決定論 transcript。全 {len(msgs)} メッセージ)\n"
    )
    # 本文内の行頭 --- / clone_visibility を無害化 (frontmatter injection 対策、life_story と同思想)
    return header + sanitize_chapter("\n".join(lines)) + "\n"


def _deliver_personal(src: Path, domain: str, label: str, dry_run: bool) -> Path:
    """personal/<pj> へ直接配送 (IMPORT_DIR 非経由 = §1.17)。"""
    from brain_wiki_helpers.domain import personal_project_dir, safe_project_slug

    pj = domain.split("/", 1)[1] if "/" in domain else ""
    # manifest の project 名は正規 slug そのものを要求 (typo/traversal が黙って
    # 別 project に化けるのを防ぐ = slug 化で変形される名前は loud に拒否)
    if not pj or safe_project_slug(pj) != pj:
        raise ValueError(f"不正な personal project 名: {pj!r}")
    target_dir = personal_project_dir(WIKI_DIR, pj)
    if target_dir is None:
        raise ValueError(f"不正な personal project 名: {pj!r}")
    dest = target_dir / "imports" / f"{src.stem}.md"
    if dry_run:
        return dest
    md = _render_personal_transcript(src, label or src.stem, pj)
    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.write_text(md, encoding="utf-8")
    return dest


def sweep(dry_run: bool = False) -> dict:
    if not INBOX.is_dir():
        return {"ok": True, "delivered": 0, "note": "inbox なし"}
    manifest = _load_manifest()
    st = _load_state()
    delivered, skipped, errors = [], 0, []

    for src in sorted(INBOX.glob("*.txt")):
        digest = _sha(src)
        if st["delivered"].get(src.name) == digest:
            skipped += 1
            continue
        entry = manifest.get(src.name, {})
        domain = entry.get("domain", "owndays")
        label = entry.get("label", "")
        try:
            if domain.startswith("personal/"):
                dest = _deliver_personal(src, domain, label, dry_run)
                route = f"personal → {dest}"
            else:
                dest = IMPORT_DIR / src.name
                route = f"owndays → IMPORT_DIR/{src.name}"
                if not dry_run:
                    IMPORT_DIR.mkdir(parents=True, exist_ok=True)
                    shutil.copy2(src, dest)
            logger.info(f"配送{'(dry)' if dry_run else ''}: {src.name} [{domain}] {route}")
            if not dry_run:
                st["delivered"][src.name] = digest
            delivered.append({"file": src.name, "domain": domain})
        except Exception as e:
            logger.exception(f"配送失敗: {src.name}")
            errors.append(f"{src.name}: {type(e).__name__}: {e}")

    if not dry_run and delivered:
        _save_state(st)

    # §1.18 loud_fail: 配送 (取込系統) の silent 死防止。成否確定点で 1 実行 1 記録
    try:
        from clone_improve_lib import loud_fail
        if errors:
            loud_fail("import_inbox", False,
                      f"chat export 配送失敗 {len(errors)} 件: {errors[0][:120]}",
                      threshold=2, cooldown_h=12)
        else:
            loud_fail("import_inbox", True)
    except Exception:
        pass

    return {"ok": not errors, "delivered": len(delivered), "skipped": skipped,
            "errors": errors, "dry_run": dry_run}


def main() -> int:
    ap = argparse.ArgumentParser(description="import_inbox → IMPORT_DIR / wiki/personal 配送")
    ap.add_argument("--dry-run", action="store_true")
    a = ap.parse_args()
    r = sweep(dry_run=a.dry_run)
    print(json.dumps(r, ensure_ascii=False))
    return 0 if r.get("ok") else 1


if __name__ == "__main__":
    raise SystemExit(main())
