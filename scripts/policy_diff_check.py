#!/usr/bin/env python3
"""開発方針 (CLAUDE.md / docs/decisions 等) 変更の Fable 5 最終チェック。

★2026-07-10 海山指示「開発方針の最終チェックも Fable 5 を通すように」。
監督者層 (litellm `supervisor` = Claude Fable 5、fallback: smart→smart-fallback) が、
直近の policy ファイル変更 commit を独立レビューし、以下を検知したら海山へ LINE 通知
(personal のみ・非critical = LW には流さない):
  ① 新ルールと既存ルールの内部矛盾
  ② docs/failure-log.md の scar (過去事故の再発防止策) と衝突する緩和
  ③ 曖昧で実行不能な規定 (判定基準が無い・責任者不明)
  ④ セキュリティ/プライバシー境界の緩み (§1.1 secrets / §1.9 PII / §1.17 domain 分離)
  ⑤ 運用負荷・cron 整合の見落とし

設計:
  - 変更が無い日は LLM 呼び出しゼロ (トークン微小の原則)
  - 冪等: state (last_sha) で増分。git 操作は read-only
  - LLM 失敗時は state を進めない (翌日同レンジを再試行) + loud_fail (§1.18)
  - dev-session 側の §1.15(d) cross-check (model=fable) の本番バックストップ。
    セッション外の変更 (Mac Studio 直編集等) も漏れなく最終チェックに乗る

cron: 07:10 daily (clone_cron.sh policy-check)。手動: python3 scripts/policy_diff_check.py [--dry-run]
"""
from __future__ import annotations

import argparse
import asyncio
import json
import logging
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from clone_improve_lib import (  # noqa: E402
    IMPROVE_DIR, call_llm, extract_json, line_push, loud_fail, supervisor_model,
)

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger("policy_diff_check")

REPO_ROOT = Path(__file__).resolve().parent.parent
STATE_PATH = IMPROVE_DIR / "policy_check_state.json"

# 「開発方針」を構成するファイル群 (= §1.15(d) の対象と同じ範囲)
POLICY_PATHS = [
    "CLAUDE.md",
    "docs/decisions/",
    "docs/development_principles.md",
    "docs/review/REVIEW_CHECKLIST.md",
]

DIFF_CHAR_CAP = 50_000
MAX_COMMITS_LISTED = 20

REVIEW_PROMPT = """あなたは OWNDAYS Personal Brain の開発方針の最終レビュアー (システム監督者)。
以下は開発ルール (CLAUDE.md / docs/decisions 等) への直近の変更 diff。独立した目で最終チェックせよ。

チェック観点 (これ以外は指摘しない):
① 内部矛盾 — 新規定が既存規定 (diff 文脈内で読める範囲) と衝突していないか
② 過去事故の再発防止策 (scar) を緩めていないか — 例: secret 平文禁止、chromadb 並行アクセス禁止、
   personal ドメイン分離、PII exclude、loud-fail 標準
③ 曖昧で実行不能な規定 — 判定基準・数値・担当が無く運用で解釈が割れるもの
④ セキュリティ/プライバシー境界の緩み — 可視性 (public/private)、admin gate、通知経路
⑤ 運用整合 — cron 登録・テスト・Key Files 索引の同時更新が規律どおりか (diff から判る範囲で)

重要: 捏造禁止。diff に無い内容を推測で指摘しない。軽微な表現差・style は指摘しない。
問題が無ければ verdict=OK で findings は空配列。

出力は次の JSON のみ:
{{"verdict": "OK" | "CONCERNS", "findings": [{{"severity": "high|mid|low", "point": "指摘 (1-2文)", "quote": "根拠となる diff 中の行 (短く)"}}]}}

--- 変更 commit 一覧 ---
{commits}

--- diff (policy ファイルのみ、{cap} 字上限) ---
{diff}
"""


def _git(args: list[str], repo: Path) -> str:
    out = subprocess.run(
        ["git", *args], cwd=repo, capture_output=True, text=True, timeout=60,
    )
    if out.returncode != 0:
        raise RuntimeError(f"git {' '.join(args[:3])} failed: {out.stderr[:200]}")
    return out.stdout


def _head_sha(repo: Path) -> str:
    return _git(["rev-parse", "HEAD"], repo).strip()


def _load_state() -> dict:
    try:
        return json.loads(STATE_PATH.read_text(encoding="utf-8"))
    except Exception:
        return {}


def _save_state(state: dict) -> None:
    STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
    STATE_PATH.write_text(json.dumps(state, ensure_ascii=False), encoding="utf-8")


def policy_commits(repo: Path, since_sha: str) -> list[str]:
    """since_sha..HEAD で policy ファイルに触れた commit (oneline) を返す。"""
    out = _git(
        ["log", "--format=%h %s", f"{since_sha}..HEAD", "--", *POLICY_PATHS], repo,
    )
    return [ln for ln in out.strip().splitlines() if ln.strip()]


def policy_diff(repo: Path, since_sha: str) -> str:
    return _git(["diff", f"{since_sha}..HEAD", "--", *POLICY_PATHS], repo)


async def review_with_supervisor(commits: list[str], diff: str) -> dict:
    prompt = REVIEW_PROMPT.format(
        commits="\n".join(commits[:MAX_COMMITS_LISTED]),
        diff=diff[:DIFF_CHAR_CAP],
        cap=DIFF_CHAR_CAP,
    )
    # temperature=None: Fable 5 は temperature 送信で 400 (fact-check 済)。
    # max_tokens 8000: thinking 常時 on で thinking tokens が max_tokens 内数 → JSON 切れ防止
    out = await call_llm(
        prompt, model=supervisor_model(), max_tokens=8000, temperature=None,
    )
    return extract_json(out)


def notify(result: dict, commits: list[str]) -> None:
    findings = result.get("findings") or []
    lines = [
        "📐 開発方針 最終チェック (Fable 5 監督者)",
        f"対象 commit {len(commits)} 件で懸念 {len(findings)} 点:",
        "",
    ]
    for f in findings[:6]:
        sev = {"high": "🔴", "mid": "🟡", "low": "⚪"}.get(f.get("severity", ""), "⚪")
        lines.append(f"{sev} {f.get('point', '')[:180]}")
        if f.get("quote"):
            lines.append(f"   > {f['quote'][:120]}")
    lines.append("")
    lines.append("(方針レビューのため通知は personal のみ。修正要否は海山判断)")
    # 方針レビューは配達保証系ではない → 非critical (LW に流さない)
    line_push("\n".join(lines))


def run(repo: Path = REPO_ROOT, dry_run: bool = False) -> int:
    state = _load_state()
    head = _head_sha(repo)
    last = state.get("last_sha", "")

    if not last:
        # 初回は前向き初期化のみ (過去分の一括レビューはしない = dev_journal と同思想)
        if not dry_run:
            _save_state({"last_sha": head})
            loud_fail("policy_diff_check", True)
        logger.info(f"初回: state を HEAD ({head[:8]}) に初期化、レビューは次回から")
        return 0

    if last == head:
        logger.info("HEAD 変化なし")
        if not dry_run:
            loud_fail("policy_diff_check", True)
        return 0

    commits = policy_commits(repo, last)
    if not commits:
        # commit は進んだが policy ファイルに変更なし → state だけ進める (LLM 0 call)
        logger.info(f"policy 変更なし ({last[:8]}..{head[:8]})、state 前進のみ")
        if not dry_run:
            _save_state({"last_sha": head})
            loud_fail("policy_diff_check", True)
        return 0

    logger.info(f"policy 変更 commit {len(commits)} 件:\n" + "\n".join(commits[:10]))
    if dry_run:
        logger.info("[dry-run] LLM レビュー・通知・state 前進は skip")
        return 0

    diff = policy_diff(repo, last)
    try:
        result = asyncio.run(review_with_supervisor(commits, diff))
    except Exception as e:
        # 失敗時は state を進めない = 翌日同レンジを再試行。silent 死は §1.18 で loud 化
        logger.error(f"supervisor レビュー失敗: {e}")
        loud_fail("policy_diff_check", False, f"LLM review failed: {e}")
        return 1

    verdict = (result.get("verdict") or "").upper()
    findings = result.get("findings") or []
    logger.info(f"verdict={verdict} findings={len(findings)}")
    if verdict == "CONCERNS" and findings:
        notify(result, commits)

    _save_state({"last_sha": head, "last_verdict": verdict,
                 "last_findings": len(findings)})
    loud_fail("policy_diff_check", True)
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description="開発方針変更の Fable5 最終チェック")
    parser.add_argument("--dry-run", action="store_true",
                        help="検出のみ (LLM/通知/state 前進なし)")
    parser.add_argument("--repo", default=str(REPO_ROOT), help="repo root (test 用)")
    args = parser.parse_args()
    return run(Path(args.repo), dry_run=args.dry_run)


if __name__ == "__main__":
    sys.exit(main())
