#!/usr/bin/env python3
"""scripts/claude_export_import.py — Claude.ai データ書き出しから Example 会話だけを personal へ import。

★2026-06-28 海山指示「export→import で MacBook 完結」。auto スクレイプ(要ログイン)の代替=確実な経路。
Claude.ai の「設定 → プライバシー → データを書き出す」で得る conversations.json(zip 内)をパースし、
**title または本文が Example marker に合致する会話だけ**を LLM 要約 → personal/example-garden/conversations/
に private 書込(claude_personal_sync の abstract/write を再利用 = 同じ隔離・捏造禁止)。

非合致会話は読むが保存しない(export は海山が意図的に共有した自分のデータ)。state で再 import 防止。

実行(Mac Studio、LITELLM 要): python3 scripts/claude_export_import.py <export.zip|conversations.json> [--dry-run]
"""
from __future__ import annotations

import argparse
import asyncio
import json
import logging
import sys
import zipfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))   # scripts/ sibling import

from claude_personal_sync import (  # noqa: E402  同じ要約・personal 書込・marker を再利用
    EXAMPLE_RE, abstract_conversation, is_example_title, write_personal_abstract,
)

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger("claude_export_import")

ROOT = Path(__file__).resolve().parents[1]
STATE_FILE = ROOT / "data" / "brain" / ".example_import_state.json"


def load_export(path: Path) -> list:
    """export(zip or conversations.json)から conversations の list を取り出す。"""
    if path.suffix == ".zip" or zipfile.is_zipfile(path):
        with zipfile.ZipFile(path) as z:
            name = next((n for n in z.namelist() if n.endswith("conversations.json")), None)
            if not name:
                raise ValueError("zip 内に conversations.json が見つかりません")
            data = json.loads(z.read(name).decode("utf-8"))
    else:
        data = json.loads(path.read_text(encoding="utf-8"))
    return data if isinstance(data, list) else data.get("conversations", [])


def _msg_text(m: dict) -> str:
    """message の本文を取り出す(新旧 export 形式に両対応: text or content[].text)。"""
    t = m.get("text") or ""
    if not t and isinstance(m.get("content"), list):
        t = " ".join(p.get("text", "") for p in m["content"] if isinstance(p, dict))
    return (t or "").strip()


def parse_conversations(data: list) -> list[dict]:
    convos = []
    for c in data:
        msgs = []
        for m in c.get("chat_messages", []) or []:
            role = "user" if (m.get("sender") == "human") else "assistant"
            text = _msg_text(m)
            if text:
                msgs.append({"role": role, "content": text})
        convos.append({"id": c.get("uuid", "") or "", "title": c.get("name") or "Untitled",
                       "messages": msgs})
    return convos


def is_example_conv(conv: dict) -> bool:
    """title 合致、または本文のどれかに Example marker(export は意図共有なので content も見る)。"""
    if is_example_title(conv.get("title", "")):
        return True
    return any(EXAMPLE_RE.search(m.get("content", "")) for m in conv.get("messages", []))


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


async def run(export_path: Path, *, dry_run: bool = False, llm=None) -> dict:
    # ★PJ 単位(projects.json)で Example Garden の全会話を対象に(title/content は fallback)。
    # 海山指示「PJ で全部」。load_export_with_projects は遅延 import(循環回避・CI 軽量)。
    from claude_export_alignment import load_export_with_projects, _project_name
    raw_convos, projects = load_export_with_projects(export_path)
    convos = parse_conversations(raw_convos)
    done = _load_state()
    example = []
    for c, raw in zip(convos, raw_convos):
        if c["id"] in done:
            continue
        pname = _project_name(raw, projects)
        if EXAMPLE_RE.search(pname or "") or is_example_conv(c):   # PJ名 Example=全部 / fallback=title・content
            example.append(c)
    logger.info(f"全{len(convos)}会話中 Example(PJ+fallback)未取込 {len(example)} 件")
    written = []
    for c in example:
        if not c["messages"]:
            continue
        abstract = await abstract_conversation(c["title"], c["messages"], llm=llm)
        if not abstract:
            continue
        if dry_run:
            print(f"\n=== {c['title']} ({c['id'][:8]}) ===\n{abstract[:600]}")
        else:
            p = write_personal_abstract(c["id"] or c["title"], c["title"], abstract)
            logger.info(f"  → {p.name}")
        done.add(c["id"]); written.append(c["id"])
    if not dry_run and written:
        _save_state(done)
        try:
            import subprocess
            subprocess.run(["python3", str(ROOT / "scripts" / "personal_snapshot.py")],
                           timeout=60, check=False)
        except Exception as e:
            logger.warning(f"snapshot 失敗: {e}")
    return {"ok": True, "example_found": len(example), "written": len(written), "dry_run": dry_run}


def main() -> int:
    ap = argparse.ArgumentParser(description="Claude.ai export → Example 会話を personal へ import")
    ap.add_argument("export", help="export.zip または conversations.json のパス")
    ap.add_argument("--dry-run", action="store_true", help="要約を表示(personal に書かない)")
    a = ap.parse_args()
    path = Path(a.export).expanduser()
    if not path.exists():
        print(f"ファイルが見つかりません: {path}"); return 1
    r = asyncio.run(run(path, dry_run=a.dry_run))
    print(r)
    return 0 if r.get("ok") else 1


if __name__ == "__main__":
    raise SystemExit(main())
