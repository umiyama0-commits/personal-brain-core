#!/usr/bin/env python3
"""scripts/claude_export_alignment.py — Claude.ai export の「アラインメント雑談」会話を人格へ取込。

★2026-06-29 海山指示「① 継続取込パイプライン」。音声 alignment(Vapi 電話)は既に人格へ自動取込
されている(main.py /webhook/voice-alignment → alignment_interview.extract_session → wiki/interview/)。
本スクリプトは **Claude.ai のテキスト「アラインメント雑談」プロジェクト**(クローン育成チャット)を、
**音声と全く同じ人格蒸留パイプライン**に流す = export の該当会話を transcript 化し
`record_session` + `extract_session` へ。結果は interview_extracted/ に **レビュー待ち**で止まる
(= 海山が採用して初めて wiki/interview/ に反映、捏造・誤取込の安全ゲートを継承)。

フィルタ: 会話の project 名 or title が ALIGN マーカー(アラインメント/雑談/クローン/align)に合致するもの。
非合致は蒸留しない。state で再取込防止。**まず --dry-run で合致会話を確認**してから本実行推奨。

実行(Mac Studio、LITELLM 要): python3 scripts/claude_export_alignment.py <export.zip|conversations.json> [--dry-run]
"""
from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
import re
import sys
import zipfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))         # scripts/ sibling
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))     # repo root (alignment_interview)

from claude_export_import import parse_conversations               # noqa: E402  export パース再利用
# alignment_interview(人格蒸留)は run() 内で遅延 import(parse/filter は不要 = CI/test を軽く保つ)

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger("claude_export_alignment")

ROOT = Path(__file__).resolve().parents[1]
STATE_FILE = ROOT / "data" / "brain" / ".alignment_import_state.json"
LITELLM_URL = os.getenv("LITELLM_URL", "http://localhost:4000")
LITELLM_KEY = os.getenv("LITELLM_KEY") or os.getenv("LITELLM_MASTER_KEY", "")

# アラインメント雑談(クローン育成チャット)の合致マーカー。env で上書き可。
ALIGN_RE = re.compile(os.getenv("ALIGNMENT_MARKER", r"アラインメント|雑談|クローン|clone|align|人格|価値観"),
                      re.IGNORECASE)


def load_export_with_projects(path: Path):
    """export(zip/json)から conversations と projects(uuid→name)を取り出す。"""
    projects: dict[str, str] = {}
    if path.suffix == ".zip" or zipfile.is_zipfile(path):
        with zipfile.ZipFile(path) as z:
            cname = next((n for n in z.namelist() if n.endswith("conversations.json")), None)
            if not cname:
                raise ValueError("zip 内に conversations.json が無い")
            data = json.loads(z.read(cname).decode("utf-8"))
            pname = next((n for n in z.namelist() if n.endswith("projects.json")), None)
            if pname:
                try:
                    for p in json.loads(z.read(pname).decode("utf-8")):
                        if p.get("uuid"):
                            projects[p["uuid"]] = p.get("name", "")
                except Exception:
                    pass
    else:
        data = json.loads(path.read_text(encoding="utf-8"))
    convos = data if isinstance(data, list) else data.get("conversations", [])
    return convos, projects


def _project_name(raw: dict, projects: dict) -> str:
    """会話の所属 project 名を best-effort で得る(export 形式差を吸収)。"""
    pj = raw.get("project")
    pu = raw.get("project_uuid") or (pj.get("uuid") if isinstance(pj, dict) else None)
    if pu and pu in projects:
        return projects[pu]
    if isinstance(pj, dict):
        return pj.get("name", "")
    return pj if isinstance(pj, str) else ""


def is_alignment_conv(conv: dict, project_name: str) -> bool:
    """project 名 or title が ALIGN マーカーに合致(本文は見ない=雑多な会話を巻き込まない)。"""
    return bool(ALIGN_RE.search(project_name or "")) or bool(ALIGN_RE.search(conv.get("title", "")))


def _to_transcript(messages: list[dict], cap: int = 22000) -> str:
    out, acc = [], 0
    for m in messages:
        who = "海山" if m.get("role") == "user" else "AI"
        line = f"{who}: {(m.get('content') or '').strip()}"
        if acc + len(line) > cap:
            break
        out.append(line); acc += len(line)
    return "\n".join(out)


def _load_state() -> set:
    if STATE_FILE.exists():
        try:
            return set(json.loads(STATE_FILE.read_text(encoding="utf-8")).get("done_ids", []))
        except Exception:
            pass
    return set()


def _save_state(done: set) -> None:
    STATE_FILE.write_text(json.dumps({"done_ids": list(done)[-2000:]}, ensure_ascii=False, indent=2),
                          encoding="utf-8")


async def run(export_path: Path, *, dry_run: bool = False) -> dict:
    import httpx
    raw_convos, projects = load_export_with_projects(export_path)
    convos = parse_conversations(raw_convos)
    done = _load_state()
    targets = []
    for c, raw in zip(convos, raw_convos):
        if c["id"] in done:
            continue
        if is_alignment_conv(c, _project_name(raw, projects)) and len(c.get("messages", [])) >= 2:
            targets.append(c)
    logger.info(f"全{len(convos)}会話中 アラインメント雑談 未取込 {len(targets)} 件")

    extracted = 0
    if not dry_run:
        import alignment_interview as ai   # 遅延 import(蒸留時のみ。Mac Studio フル環境)
    async with httpx.AsyncClient(timeout=180.0) as http:
        for c in targets:
            transcript = _to_transcript(c["messages"])
            if len(transcript) < 80:
                continue
            if dry_run:
                print(f"\n=== {c['title']} ({c['id'][:8]}) — {len(c['messages'])} msgs ===\n{transcript[:400]}")
                continue
            try:
                ai.record_session(transcript, source="claude-alignment")
                await ai.extract_session(transcript, http, LITELLM_URL, LITELLM_KEY,
                                         raw_filename=f"claude-{c['id'][:8]}")
                logger.info(f"  蒸留→レビュー待ち: {c['title']}")
                extracted += 1
                done.add(c["id"])
            except Exception as e:
                logger.warning(f"  抽出失敗 {c['title']}: {type(e).__name__}: {e}")
    if not dry_run and extracted:
        _save_state(done)
    return {"ok": True, "matched": len(targets), "extracted": extracted, "dry_run": dry_run,
            "note": "採用は既存フロー(interview_extracted のレビュー→ apply_extraction)で海山が実施"}


def main() -> int:
    ap = argparse.ArgumentParser(description="Claude.ai export → アラインメント雑談を人格へ蒸留(レビュー待ち)")
    ap.add_argument("export", help="export.zip または conversations.json")
    ap.add_argument("--dry-run", action="store_true", help="合致会話を表示(蒸留しない)")
    a = ap.parse_args()
    path = Path(a.export).expanduser()
    if not path.exists():
        print(f"ファイル無し: {path}"); return 1
    r = asyncio.run(run(path, dry_run=a.dry_run))
    print(r)
    return 0 if r.get("ok") else 1


if __name__ == "__main__":
    raise SystemExit(main())
