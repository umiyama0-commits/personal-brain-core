#!/usr/bin/env python3
"""GitHub Actions (main) の赤を海山へ通知する (★2026-07-10 世界基準評価 #3)。

背景: CI(tests.yml)は「壊れたら赤で気付く」signal 層のはずが、2026-07-03 から
typecheck/pytest-fastapi が恒常赤のまま**無通知**で 40+ commit が無テスト本番反映されていた。
「赤で気付く」層自体が1週間死んでいた。本 script が最新 main run の conclusion を毎日 poll し、
赤に転じた時だけ通知 (edge-triggered = 同じ赤で毎日鳴らさない)。

- gh CLI 認証済み前提 (repo scope)。未認証/未導入なら loud_fail (§1.18、silent 死にしない)。
- 通知は line_push (非critical = 海山 personal LINE のみ、dev signal。LW=社員公開には流さない)。
- state で last-notified conclusion+sha を持ち、赤→緑→赤 の再燃は再通知、赤継続は無音。

cron: clone_cron.sh ci-check (07:15 daily、policy-check の後)。手動: python3 scripts/ci_status_check.py [--dry-run]
"""
from __future__ import annotations

import argparse
import json
import logging
import os
import shutil
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from clone_improve_lib import IMPROVE_DIR, line_push, loud_fail  # noqa: E402

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger("ci_status_check")

STATE_PATH = IMPROVE_DIR / "ci_status_state.json"
REPO = "your-org/your-repo"  # origin (private)。gh は origin remote から自動解決も可
BRANCH = "main"


def _resolve_gh() -> str:
    """gh バイナリを解決。cron 最小 PATH (/usr/local/bin) に homebrew が無いため
    (§1.8)、PATH 依存せず絶対 path も探す。見つからなければ例外。"""
    p = shutil.which("gh")
    if p:
        return p
    for cand in ("/opt/homebrew/bin/gh", "/usr/local/bin/gh",
                 str(Path.home() / ".local/bin/gh")):
        if Path(cand).exists():
            return cand
    raise RuntimeError("gh CLI が見つからない (PATH + 既知 path 全滅)")


def _gh_latest_run() -> dict | None:
    """main の最新 workflow run を返す。gh 不在/未認証は例外。

    ★2026-07-10: host の gh CLI token 失効時の保険として、`.env` の GH_TOKEN / GITHUB_TOKEN
    があれば gh に env 経由で渡す (gh は GH_TOKEN を優先採用 = CLI 再認証なしで復旧できる)。
    """
    gh = _resolve_gh()
    env = dict(os.environ)
    tok = os.getenv("GH_TOKEN") or os.getenv("GITHUB_TOKEN") or ""
    if tok:
        env["GH_TOKEN"] = tok
    out = subprocess.run(
        [gh, "run", "list", "-R", REPO, "--branch", BRANCH, "--limit", "1",
         "--json", "conclusion,status,headSha,displayTitle,url,workflowName"],
        capture_output=True, text=True, timeout=60, env=env,
    )
    if out.returncode != 0:
        raise RuntimeError(f"gh run list failed: {out.stderr[:200]}")
    runs = json.loads(out.stdout or "[]")
    return runs[0] if runs else None


def _load_state() -> dict:
    try:
        return json.loads(STATE_PATH.read_text(encoding="utf-8"))
    except Exception:
        return {}


def _save_state(state: dict) -> None:
    STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
    STATE_PATH.write_text(json.dumps(state, ensure_ascii=False), encoding="utf-8")


def run(dry_run: bool = False) -> int:
    try:
        latest = _gh_latest_run()
    except Exception as e:
        # gh 不在/未認証/API 断 = checker 自体の silent 死を防ぐ (§1.18)
        logger.error(f"gh 取得失敗: {e}")
        if not dry_run:
            loud_fail("ci_status_check", False,
                      f"gh 取得失敗 (CI 監視が効いていない): {e}", cooldown_h=48)
        return 1

    if not latest:
        logger.info("run が無い")
        if not dry_run:
            loud_fail("ci_status_check", True)
        return 0

    status = latest.get("status")      # queued / in_progress / completed
    conclusion = latest.get("conclusion")  # success / failure / cancelled / None
    sha = (latest.get("headSha") or "")[:8]
    logger.info(f"latest run: status={status} conclusion={conclusion} sha={sha}")

    if status != "completed":
        # 実行中は判定を次回へ (state 不変)
        if not dry_run:
            loud_fail("ci_status_check", True)
        return 0

    state = _load_state()
    if dry_run:
        logger.info(f"[dry-run] conclusion={conclusion} last_notified_sha={state.get('notified_sha')}")
        return 0

    if conclusion == "success":
        # 緑に戻ったら state をクリア (次の赤で再通知できるように) + 前回赤なら復旧通知
        if state.get("notified_sha"):
            line_push(f"✅ CI 復旧 (main 緑): {latest.get('displayTitle', '')[:60]} [{sha}]")
        _save_state({"last_conclusion": "success"})
        loud_fail("ci_status_check", True)
        return 0

    # 赤 (failure / cancelled / timed_out 等)
    if state.get("notified_sha") == sha:
        logger.info("同一 sha で通知済み → 無音 (edge-triggered)")
        loud_fail("ci_status_check", True)
        return 0

    line_push(
        f"🔴 CI が赤: {BRANCH} ({latest.get('workflowName', 'tests')})\n"
        f"{latest.get('displayTitle', '')[:70]}\n"
        f"conclusion={conclusion} [{sha}]\n"
        f"{latest.get('url', '')}\n"
        f"→ 無テストで本番反映が続く前に確認を (gh run view --log-failed)"
    )
    _save_state({"last_conclusion": conclusion, "notified_sha": sha})
    loud_fail("ci_status_check", True)  # checker 自体は正常動作
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description="GitHub Actions main の赤を海山へ通知")
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()
    return run(dry_run=args.dry_run)


if __name__ == "__main__":
    sys.exit(main())
