"""services/_review_store.py — clone_review 系 jsonl queue の並行安全プリミティブ.

★2026-06-10: system_issues / data_gaps / web_clips は read-modify-write (status 更新・
dedupe・comment 追記) に lock が無く、別プロセス (dashboard を持つ line-bot と、cron の
synthetic_employee_agent / auto_improve) の near-concurrent 更新で lost update / 部分読み
が起きえた (ADR Codex MEDIUM)。fcntl ファイルロックで RMW を直列化し、全体書き込みは
tmp + os.replace で atomic 化する。

使い方:
    from services._review_store import locked, read_jsonl, write_jsonl_atomic, append_jsonl
    with locked(FILE):
        items = read_jsonl(FILE, logger)
        ... modify items ...
        write_jsonl_atomic(FILE, items)
"""
from __future__ import annotations

import fcntl
import json
import os
from contextlib import contextmanager
from pathlib import Path


@contextmanager
def locked(path: Path):
    """path への read-modify-write を直列化する advisory file lock (fcntl.LOCK_EX).

    同一ホストの全プロセスが同じ lock file (<path>.lock) を排他取得すれば lost update を
    防げる。RMW は短時間 (数ms) なので保持時間も短く、async ハンドラから呼んでもブロックは
    軽微。lock file は残置しても無害 (中身は空、再利用される)。
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    lock_path = Path(f"{path}.lock")
    lf = lock_path.open("w")
    try:
        fcntl.flock(lf.fileno(), fcntl.LOCK_EX)
        yield
    finally:
        try:
            fcntl.flock(lf.fileno(), fcntl.LOCK_UN)
        finally:
            lf.close()


def read_jsonl(path: Path, logger=None) -> list:
    """jsonl を行ごとに parse して list で返す (壊れた行は skip)。"""
    if not path.exists():
        return []
    items = []
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            items.append(json.loads(line))
        except Exception as e:
            if logger is not None:
                logger.warning(f"{path.name} invalid line: {e}")
    return items


def write_jsonl_atomic(path: Path, items: list) -> None:
    """全 items を tmp に書いて os.replace で atomic に差し替える (部分読み防止)。

    lost update 防止は呼び出し側 locked() の責務。tmp 名に pid を付け同時実行の衝突を避ける。
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    lines = [json.dumps(r, ensure_ascii=False) for r in items]
    tmp = Path(f"{path}.tmp.{os.getpid()}")
    tmp.write_text("\n".join(lines) + ("\n" if lines else ""), encoding="utf-8")
    os.replace(tmp, path)


def append_jsonl(path: Path, rec: dict) -> None:
    """1 レコードを append (update との競合を避けるため locked() 下で呼ぶ前提)。"""
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as f:
        f.write(json.dumps(rec, ensure_ascii=False) + "\n")


def write_text_atomic(path: Path, text: str) -> None:
    """任意テキストを tmp + os.replace で atomic 書き込み (.json/.md の全文上書き用)。

    jsonl でない dict(.json)/wiki(.md) の torn read を防ぐ。lost update 防止は locked() の責務。
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = Path(f"{path}.tmp.{os.getpid()}")
    tmp.write_text(text, encoding="utf-8")
    os.replace(tmp, path)
