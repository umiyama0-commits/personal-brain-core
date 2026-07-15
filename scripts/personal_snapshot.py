#!/usr/bin/env python3
"""scripts/personal_snapshot.py — personal ドメインの版管理(保管)。

★2026-06-28 海山「保管機能」。wiki/personal/ (非OWNDAYS の投資/PJ) は §1.14 で gitignore =
本体 git に履歴が無く Mac Studio SSD のみに存在 (offsite restic が唯一の保全)。不可逆な個人知識なので
**入れ子の専用 git repo** で版管理 → 瞬時 rollback + diff + 履歴閲覧。入れ子 .git は親 .gitignore
(data/brain/wiki/*) に隠れて独立、restic (data/brain 全体) が .git ごと offsite 保全する。

実行: python3 scripts/personal_snapshot.py [--list|--restore <commit>|--check] (host cron 日次)
"""
from __future__ import annotations

import argparse
import os
import subprocess
import sys
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PERSONAL_DIR = ROOT / "data" / "brain" / "wiki" / "personal"
_IDENT = ("-c", "user.email=brain@local", "-c", "user.name=personal-snapshot")


def _git(*args):
    return subprocess.run(["git", "-C", str(PERSONAL_DIR), *args],
                          capture_output=True, text=True, timeout=60)


def ensure_repo() -> bool:
    if not PERSONAL_DIR.exists():
        return False
    if (PERSONAL_DIR / ".git").exists():
        return True
    _git("init", "-q")
    _git("config", "user.email", "brain@local")
    _git("config", "user.name", "personal-snapshot")
    return (PERSONAL_DIR / ".git").exists()


def snapshot(message: str | None = None) -> dict:
    """personal/ の現状を 1 commit にする (変更が無ければ no-op)。"""
    if not ensure_repo():
        return {"ok": False, "reason": "personal/ 無し or git init 失敗"}
    _git("add", "-A")
    if not _git("status", "--porcelain").stdout.strip():
        return {"ok": True, "changed": False}
    msg = message or f"snapshot {datetime.now().strftime('%Y-%m-%d %H:%M')}"
    r = _git(*_IDENT, "commit", "-q", "-m", msg)
    return {"ok": r.returncode == 0, "changed": True, "message": msg, "err": r.stderr[:200]}


def list_snapshots(n: int = 20) -> str:
    if not (PERSONAL_DIR / ".git").exists():
        return "(版管理 未初期化 — まだ snapshot 無し)"
    return _git("log", f"-{n}", "--pretty=%h %ci %s").stdout.strip() or "(snapshot 無し)"


def restore(commit: str) -> dict:
    """過去 snapshot へ personal/ を復元 (復元前に安全 snapshot を取る)。"""
    if not (PERSONAL_DIR / ".git").exists():
        return {"ok": False, "reason": "版管理 未初期化"}
    snapshot("pre-restore safety snapshot")
    r = _git("checkout", commit, "--", ".")
    return {"ok": r.returncode == 0, "restored": commit, "err": r.stderr[:200]}


def check_restic() -> dict:
    return {"restic_env": bool(os.getenv("RESTIC_REPOSITORY")) and bool(os.getenv("RESTIC_PASSWORD"))}


def main() -> int:
    ap = argparse.ArgumentParser(description="personal ドメイン版管理 (保管、host cron)")
    ap.add_argument("--list", action="store_true", help="snapshot 履歴")
    ap.add_argument("--restore", metavar="COMMIT", help="過去 snapshot へ復元")
    ap.add_argument("--check", action="store_true", help="restic 設定の有無")
    a = ap.parse_args()
    if a.list:
        print(list_snapshots()); return 0
    if a.restore:
        print(restore(a.restore)); return 0
    if a.check:
        print(check_restic()); return 0
    r = snapshot()
    print(r)
    if not check_restic()["restic_env"]:
        print("WARNING: RESTIC_REPOSITORY/PASSWORD 未設定 = personal の offsite 保全が無い (RPO∞)",
              file=sys.stderr)
    return 0 if r.get("ok") else 1


if __name__ == "__main__":
    raise SystemExit(main())
