#!/usr/bin/env python3
"""
claude_dispatcher.py — LINE → Claude Code ブリッジワーカー

Mac ホスト上で常駐し、Docker コンテナ (main.py) が書き出す
  data/brain/claude_tasks/pending/*.json
を監視。タスクを拾ったら `claude -p` を --dangerously-skip-permissions 付きで
起動し、stdout を LINE Push API でユーザーに返す。

起動:
  python3 claude_dispatcher.py

launchd 化する場合は同梱の claude-dispatcher.plist を参照。

環境変数:
  LINE_CHANNEL_ACCESS_TOKEN  — LINE Push API
  CLAUDE_BIN                 — claude CLI のパス (default: "claude")
  CLAUDE_DISPATCHER_CWD      — 作業ディレクトリ (default: brain-agent/)
  CLAUDE_TIMEOUT             — 1タスクの最大秒数 (default: 900 = 15分)
  CLAUDE_DISPATCHER_POLL     — pending ポーリング間隔秒 (default: 3)
"""

import os
import sys
import json
import time
import subprocess
import logging
from pathlib import Path
from datetime import datetime

try:
    import requests
except ImportError:
    print("pip install requests が必要です", file=sys.stderr)
    sys.exit(1)

try:
    from dotenv import load_dotenv
    load_dotenv(Path(__file__).parent / ".env")
except ImportError:
    pass

PROJECT_ROOT = Path(__file__).parent
BRAIN_ROOT = PROJECT_ROOT / "data" / "brain"
TASKS_DIR = BRAIN_ROOT / "claude_tasks"
PENDING = TASKS_DIR / "pending"
RUNNING = TASKS_DIR / "running"
DONE = TASKS_DIR / "done"
FAILED = TASKS_DIR / "failed"

for d in (PENDING, RUNNING, DONE, FAILED):
    d.mkdir(parents=True, exist_ok=True)

LINE_TOKEN = os.environ.get("LINE_CHANNEL_ACCESS_TOKEN", "")
CLAUDE_BIN = os.environ.get("CLAUDE_BIN", "claude")
CLAUDE_CWD = os.environ.get("CLAUDE_DISPATCHER_CWD", str(PROJECT_ROOT))
POLL_INTERVAL = int(os.environ.get("CLAUDE_DISPATCHER_POLL", "3"))
CLAUDE_TIMEOUT = int(os.environ.get("CLAUDE_TIMEOUT", "900"))

# Webhook (main.py) へ結果を POST するためのエンドポイント
BRAIN_WEBHOOK_URL = os.environ.get(
    "BRAIN_WEBHOOK_URL", "http://localhost:8000"
)
BRAIN_EXTENSION_KEY = os.environ.get("BRAIN_EXTENSION_KEY", "")


PLAN_SYSTEM_PROMPT = """あなたは OWNDAYS CEO の Personal Brain システム
(/Users/brain/brain-agent/, Python/FastAPI) の保守担当です。

## Coding Discipline (Karpathy guidelines、★2026-05-23 LEE §6.5 導入)

すべての改修計画は以下 4 原則に従って立案すること:

1. **Think Before Coding** — 仮定を明示する。不確実なら止まって質問する。複数解釈が
   あるなら勝手に選ばず「A か B か」を計画段階で出す。
2. **Simplicity First** — 要求以上の機能・抽象化・「将来の柔軟性」・起こり得ないケースの
   エラー処理を入れない。200 行で書けるなら 50 行を目指す。新ファイル・新 module 追加前に
   「既存に追記で済まないか」を 1 度問う。
3. **Surgical Changes** — 触る場所だけ修正する。隣接コードの「改善」をしない。すべての
   変更行はユーザー指示まで遡って追跡可能であること。
4. **Goal-Driven Execution** — 作業を検証可能な目標に変換する (「動くようにして」ではなく
   「このテストが通る」「この curl が 200 を返す」)。

これらに反する計画 (= 過剰な抽象化、無関係なリファクタ混入、不明瞭な完了条件) は
**plan 段階で却下される**。改修指示の本質を見極め、最小コストで達成する案を立てる。

---

以下の改修指示を受けましたが、**まだコードを変更しないでください**。
まず調査と計画のみを行い、以下を日本語で簡潔に出力してください
（見出しはそのまま使う、前置き不要）:

## 理解した改修内容
（ユーザー指示を自分の言葉で 2〜3 行に要約）

## 影響範囲
（変更対象のファイル・関数を箇条書き）

## 変更案
（何をどう書き換えるかの要点。3〜6 項目の箇条書き、surgical を意識）

## リスク・注意点
（壊す可能性のあるもの、確認が必要な点。なければ「特になし」）

## 検証方法
（変更後に「成功」と判定するコマンド or 観察対象を 1-2 個。「動くこと」では不可）

## 所要時間の目安
（◯分程度 / 即時）

---
改修指示:
"""


EXECUTE_SYSTEM_PROMPT = """あなたは OWNDAYS CEO の Personal Brain システム
(/Users/brain/brain-agent/, Python/FastAPI) の保守担当です。

## Coding Discipline (Karpathy guidelines、★2026-05-23 LEE §6.5 導入)

実装は以下 4 原則を厳守:

1. **Think Before Coding** — 計画にない判断を勝手にしない。曖昧な場面で迷ったら止まって
   明示する (= ファイルに `# ★Q: <疑問>` でコメント残す、push 報告でも触れる)。
2. **Simplicity First** — 計画より複雑にしない。200 行を 50 行に縮められると気付いたら
   縮めてから commit。bloated abstraction / 「将来の柔軟性」用コードは入れない。
3. **Surgical Changes** — **承認済み計画にある場所だけ触る**。隣接コードの整理・
   リファクタを ad-hoc に混入させない (= 別 commit / 別計画として後送り)。
   無関係 `whitespace` 修正・コメント追加・rename もしない。
4. **Goal-Driven Execution** — 完了報告に **「実行した検証コマンド + 結果」を必ず含む**。
   「動くようにした」だけでは不十分。`pytest` / `python -m py_compile` / curl 200 等の
   実証を出す。検証が走っていなければ「未検証」と明示。

## Verification Before Completion (★2026-05-23 LEE §6.5 導入)

「完了」と主張する前に以下を必ず実行:
- 編集した .py には `python3 -m py_compile <file>` を走らせる
- 既存 test がある module は `python3 -m pytest tests/smoke/test_<module>.py -q` を走らせる
- shell script は `bash -n <file>` で syntax check
- 結果を「検証」セクションに行頭 ✓ or ✗ で列挙する

---

以下のユーザー指示と、**既にユーザーが承認済みの計画**があります。
計画に沿って実装してください。完了後、以下を日本語で簡潔に報告してください
（見出しはそのまま使う、前置き不要）:

## 変更したファイル
（相対パスを箇条書き、計画範囲外への touch は理由を明示）

## 主な変更内容
（3〜6 項目の箇条書き、surgical を意識した変更のみ）

## 検証
（実行コマンド + 結果を行頭 ✓ / ✗ で。例:
 ✓ python3 -m py_compile main.py
 ✓ python3 -m pytest tests/smoke/test_admin_gate.py -q (10 passed)
 ✗ docker compose build (未実行、host で実行不可)）

## 残課題
（あれば surgical の都合上後送りしたもの含めて。なければ「なし」）
"""

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
)
log = logging.getLogger("claude-dispatcher")


# ─── LINE Push ───
def push_line(user_id: str, text: str) -> None:
    if not LINE_TOKEN or not user_id:
        log.info(f"skip LINE push (token/uid missing): {text[:80]}")
        return
    chunks = [text[i:i + 4500] for i in range(0, len(text), 4500)][:5]
    messages = [{"type": "text", "text": c} for c in chunks]
    try:
        r = requests.post(
            "https://api.line.me/v2/bot/message/push",
            headers={
                "Authorization": f"Bearer {LINE_TOKEN}",
                "Content-Type": "application/json",
            },
            json={"to": user_id, "messages": messages},
            timeout=15,
        )
        if r.status_code >= 300:
            log.warning(f"LINE push failed {r.status_code}: {r.text[:200]}")
    except Exception as e:
        log.warning(f"LINE push error: {e}")


# ─── Claude Code 起動 ───
def build_prompt(task: dict) -> str:
    """mode に応じて claude -p に渡す prompt を組み立てる"""
    mode = task.get("mode", "plan")
    instruction = (task.get("instruction") or "").strip()
    if mode == "execute":
        approved_plan = (task.get("approved_plan") or "").strip()
        return (
            f"{EXECUTE_SYSTEM_PROMPT}\n\n"
            f"---\n改修指示:\n{instruction}\n\n"
            f"---\n承認済み計画:\n{approved_plan}\n"
        )
    # default: plan
    return f"{PLAN_SYSTEM_PROMPT}\n{instruction}\n"


def run_claude(prompt: str) -> dict:
    """claude -p で非対話実行。--dangerously-skip-permissions で許可スキップ。"""
    start = time.time()
    cmd = [
        CLAUDE_BIN,
        "-p", prompt,
        "--dangerously-skip-permissions",
    ]
    log.info(f"running {CLAUDE_BIN} -p (cwd={CLAUDE_CWD}, prompt_len={len(prompt)})")
    try:
        proc = subprocess.run(
            cmd,
            cwd=CLAUDE_CWD,
            capture_output=True,
            text=True,
            timeout=CLAUDE_TIMEOUT,
            env={**os.environ},
        )
        elapsed = time.time() - start
        return {
            "ok": proc.returncode == 0,
            "returncode": proc.returncode,
            "stdout": proc.stdout or "",
            "stderr": proc.stderr or "",
            "elapsed": round(elapsed, 1),
        }
    except subprocess.TimeoutExpired as e:
        return {
            "ok": False,
            "error": f"timeout after {CLAUDE_TIMEOUT}s",
            "stdout": (e.stdout or "")[:4000] if isinstance(e.stdout, str) else "",
            "elapsed": CLAUDE_TIMEOUT,
        }
    except FileNotFoundError:
        return {"ok": False, "error": f"{CLAUDE_BIN} not found in PATH"}
    except Exception as e:
        return {"ok": False, "error": str(e)}


# ─── Webhook 通知 (main.py /api/claude/notify) ───
def notify_webhook(task: dict, result: dict) -> bool:
    """Claude Code の結果を main.py に POST して、LINE Push (+ Quick Reply) を任せる"""
    if not BRAIN_EXTENSION_KEY:
        log.warning("BRAIN_EXTENSION_KEY not set — fallback to direct LINE push")
        return False
    payload = {
        "task_id": task.get("task_id", ""),
        "user_id": task.get("user_id", ""),
        "mode": task.get("mode", "plan"),
        "instruction": task.get("instruction", ""),
        "ok": result.get("ok", False),
        "stdout": result.get("stdout", ""),
        "stderr": result.get("stderr", ""),
        "error": result.get("error", ""),
        "elapsed": result.get("elapsed", 0),
    }
    try:
        r = requests.post(
            f"{BRAIN_WEBHOOK_URL.rstrip('/')}/api/claude/notify",
            headers={
                "Authorization": f"Bearer {BRAIN_EXTENSION_KEY}",
                "Content-Type": "application/json",
            },
            json=payload,
            timeout=30,
        )
        if r.status_code >= 300:
            log.warning(
                f"notify webhook failed {r.status_code}: {r.text[:200]}"
            )
            return False
        return True
    except Exception as e:
        log.warning(f"notify webhook error: {e}")
        return False


# ─── タスク処理 ───
def process_task(task_path: Path) -> None:
    try:
        task = json.loads(task_path.read_text(encoding="utf-8"))
    except Exception as e:
        log.warning(f"parse error {task_path.name}: {e}")
        task_path.rename(FAILED / task_path.name)
        return

    user_id = task.get("user_id", "")
    instruction = (task.get("instruction") or "").strip()
    mode = task.get("mode", "plan")
    if not instruction:
        log.warning(f"empty instruction: {task_path.name}")
        task_path.rename(FAILED / task_path.name)
        return

    # pending → running
    running_path = RUNNING / task_path.name
    try:
        task_path.rename(running_path)
    except Exception as e:
        log.warning(f"rename to running failed: {e}")
        return

    # 開始通知
    preview = instruction.replace("\n", " ")[:100]
    mode_label = "計画作成中" if mode == "plan" else "実装実行中"
    push_line(
        user_id,
        f"⚙️ Claude Code {mode_label}\n「{preview}…」\n"
        f"（最大 {CLAUDE_TIMEOUT // 60} 分）",
    )

    log.info(f"task start: {task_path.name} mode={mode} user={user_id[:8]}")
    prompt = build_prompt(task)
    result = run_claude(prompt)
    log.info(
        f"task done:  {task_path.name} ok={result.get('ok')} "
        f"{result.get('elapsed', '?')}s"
    )

    task["result"] = result
    task["completed_at"] = datetime.now().isoformat()

    # done/ に退避
    done_path = DONE / task_path.name
    try:
        done_path.write_text(
            json.dumps(task, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        running_path.unlink(missing_ok=True)
    except Exception as e:
        log.warning(f"done write failed: {e}")

    # 結果通知: webhook 優先、失敗したら直接 LINE Push にフォールバック
    if notify_webhook(task, result):
        return

    # --- webhook 不達時のフォールバック (Quick Reply なし) ---
    if result.get("ok"):
        body = (result.get("stdout") or "").strip() or "(出力なし)"
        if len(body) > 3800:
            body = body[:3800] + "\n\n…(省略)"
        elapsed = result.get("elapsed", 0)
        label = "📋 計画" if mode == "plan" else "✅ 実装完了"
        push_line(
            user_id,
            f"{label} ({elapsed}s)\n━━━━━━━━━━━━━━━\n{body}\n\n"
            "⚠️ webhook 不達のため承認ボタンは出ません",
        )
    else:
        err = (
            result.get("error")
            or result.get("stderr")
            or result.get("stdout")
            or "unknown"
        )
        if len(err) > 3800:
            err = err[:3800] + "…"
        push_line(user_id, f"⚠️ Claude Code 失敗 ({mode})\n{err}")


def main():
    log.info(
        f"claude-dispatcher started "
        f"(cwd={CLAUDE_CWD}, bin={CLAUDE_BIN}, poll={POLL_INTERVAL}s, "
        f"timeout={CLAUDE_TIMEOUT}s)"
    )
    while True:
        try:
            pending = sorted(PENDING.glob("*.json"))
            for p in pending:
                process_task(p)
        except KeyboardInterrupt:
            log.info("interrupted, exiting")
            return
        except Exception as e:
            log.exception(f"main loop error: {e}")
        time.sleep(POLL_INTERVAL)


if __name__ == "__main__":
    main()
