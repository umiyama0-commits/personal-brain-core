"""
clone_sleep_time_agent.py — Letta sleep-time pattern (項目 4)

応答 idle 時 (会話終了 30 秒後) に裏で memory を整理する agent。

設計:
- main.py の webhook handler から `schedule_sleep_time_agent(user_id)` が呼ばれる
- 既存の idle task があれば cancel (次のターンが来たから)
- 新しい debounce timer 起動、30 秒後に sleep_time_run() が動く
- 連続会話中は debounce で cancel され続けるので呼ばれない
- 会話が一区切りすると初めて memory 整理が走る

既存 clone_memory.update_clone_memory (応答直後 background task) と並行:
- update_clone_memory: 応答直後の差分更新 (fast-gpt、軽量)
- sleep_time_run: idle 30 秒後の全体再整理 (smart、思考深め)

standalone でも動く:
  python3 scripts/clone_sleep_time_agent.py --user-id <id>
  python3 scripts/clone_sleep_time_agent.py --user-id <id> --dry-run
"""
from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
import re
import sys
from pathlib import Path
from typing import Optional

sys.path.insert(0, str(Path(__file__).resolve().parent))
from clone_improve_lib import call_llm, line_push, line_push_digest

logger = logging.getLogger("clone_sleep_time_agent")

# Debounce 管理 (in-memory dict)
# bot restart で消えるが、それで OK (idle 検知は 30 秒、restart 時は当然リセット)
_idle_tasks: dict[str, asyncio.Task] = {}

# パス (clone_history.py / clone_memory.py と同じ規約)
BRAIN_ROOT = Path(os.getenv("BRAIN_ROOT", "/app/data/brain"))
HISTORY_DIR = BRAIN_ROOT / "clone_history"
MEMORY_DIR = BRAIN_ROOT / "clone_memory"
DRAFTS_DIR = BRAIN_ROOT / "clone_improve" / "sleep_time_drafts"

# Debounce / 制限
DEFAULT_DEBOUNCE_SEC = 30
MIN_TURNS_TO_RUN = 4         # 4 ターン以上の会話だけ再整理する (1-2 ターンの軽い問は無視)
HISTORY_WINDOW = 20          # 直近 20 record を見る


def _user_file_safe(user_id: str, dir_: Path, suffix: str) -> Path:
    safe = user_id.replace("/", "_").replace("..", "_")
    return dir_ / f"{safe}{suffix}"


def _load_recent_history(user_id: str, n: int = HISTORY_WINDOW) -> list[dict]:
    """clone_history から直近 n 件読む。"""
    p = _user_file_safe(user_id, HISTORY_DIR, ".jsonl")
    if not p.exists():
        return []
    try:
        lines = p.read_text(encoding="utf-8").strip().split("\n")[-n:]
    except Exception:
        return []
    out = []
    for line in lines:
        if not line.strip():
            continue
        try:
            out.append(json.loads(line))
        except Exception:
            continue
    return out


def _load_memory(user_id: str) -> str:
    """clone_memory/<user_id>.md を読む。"""
    p = _user_file_safe(user_id, MEMORY_DIR, ".md")
    if not p.exists():
        return ""
    try:
        return p.read_text(encoding="utf-8")
    except Exception:
        return ""


def _save_memory(user_id: str, content: str):
    """clone_memory/<user_id>.md に書き戻す (tmp+os.replace で atomic、torn write 防止)。"""
    p = _user_file_safe(user_id, MEMORY_DIR, ".md")
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = Path(f"{p}.tmp.{os.getpid()}")
    tmp.write_text(content, encoding="utf-8")
    os.replace(tmp, p)


# ─── memory 全文上書きの破壊防止 (★2026-06-07 エージェント評価) ──────────────────
# sleep_time は memory を **全文上書き** するので、LLM が誤って大幅短縮した updated を返すと
# 記憶が大量喪失する。激減を guard し、上書き前に 1 世代 backup を残す。
BACKUP_DIR = BRAIN_ROOT / "clone_memory_backup"
SHRINK_GUARD_MIN_CHARS = 500   # これ未満の memory は guard 対象外 (小さい memory の正常整理を妨げない)
SHRINK_GUARD_RATIO = 0.5       # 既存の 50% 未満へ激減したら採用しない (drafts に退避)


def _should_accept_update(existing: str, updated: str) -> tuple[bool, str]:
    """memory 上書きを採用してよいか判定。激減 (大量喪失) を拒否。

    Returns: (accept, reason)。accept=False の reason は empty / no_change / shrink_guard:... 。
    """
    if not updated or not updated.strip():
        return False, "empty"
    if updated == existing:
        return False, "no_change"
    if existing and len(existing) >= SHRINK_GUARD_MIN_CHARS:
        ratio = len(updated) / max(1, len(existing))
        if ratio < SHRINK_GUARD_RATIO:
            return False, f"shrink_guard ({len(existing)}->{len(updated)}字, {ratio:.0%})"
    return True, "ok"


def _backup_memory(user_id: str, content: str) -> None:
    """上書き前の memory を 1 世代 backup (per-user、last-good を常に復元可能に)。"""
    if not content:
        return
    try:
        BACKUP_DIR.mkdir(parents=True, exist_ok=True)
        _user_file_safe(user_id, BACKUP_DIR, ".md").write_text(content, encoding="utf-8")
    except Exception as e:
        logger.warning(f"_backup_memory failed: {e}")


def _save_shrink_draft(user_id: str, existing: str, updated: str) -> None:
    """激減で reject した updated を drafts に退避 (海山が確認・手動採用できるように)。"""
    try:
        DRAFTS_DIR.mkdir(parents=True, exist_ok=True)
        from datetime import datetime, timezone, timedelta
        _jst = timezone(timedelta(hours=9))
        ts = datetime.now(_jst).strftime("%Y-%m-%d_%H%M")
        p = DRAFTS_DIR / f"{ts}-{user_id[:8]}-SHRINK.md"
        p.write_text(
            f"# ⚠️ sleep_time 激減 reject ({user_id[:12]}, {len(existing)}->{len(updated)}字)\n\n"
            f"## 既存 memory ({len(existing)}字)\n\n{existing}\n\n"
            f"## LLM 提案 (激減のため未採用、{len(updated)}字)\n\n{updated}\n",
            encoding="utf-8",
        )
        logger.warning(f"[{user_id[:8]}] shrink-reject draft -> {p.name}")
    except Exception as e:
        logger.warning(f"_save_shrink_draft failed: {e}")


SLEEP_TIME_PROMPT = """あなたは「うみやまAI」の sleep-time 整理エージェント。
社員 1 名との会話が一区切りつきました (30 秒以上次のターン無し)。
直近の会話を踏まえて、この社員の memory ファイルを **再整理** してください。

【現在日付】 {today}

【既存 memory ファイル】
{existing_memory}

【直近会話 (古→新)】
{conversation}

# 任務

1. 既存 memory の中身を読み込み、会話で **新しく分かったこと** を統合
2. **矛盾を検出** したら旧記述を更新 (新しい情報優先)
3. 4 セクションを維持:
   - Profile (役職 / 所属 / 関係性)
   - Ongoing Topics (進行中の話題 3-5 件)
   - Key Facts (確定した事実 5-10 件)
   - Preferences (応答スタイル好み)
4. **★Tier 1: date metadata 必須** — 各 Ongoing Topics / Key Facts item 末尾に
   発生 / 確認日を `(YYYY-MM-DD)` 形式で付ける。
   - 新規追加項目: 現在日付 `({today})` を付与
   - 既存項目の更新: 元の日付は保持、追加情報部分に新日付付与
   - 例: "- 龍仁モール出店 (2026-05-15)、店長候補 3 名 (2026-05-24)"
   - 例: "- 営業部所属 (2026-04-10)、現職 5 年目 (2026-04-10)"
5. **古くなった情報は archive** — 30 日以上古い Ongoing は `(過去 YYYY-MM-DD)` 形式で
   Key Facts に降格、または「以前は〜だったが今は〜」 形式で update
6. 個人特定リスク高い情報 (健康深刻 / 家族プライベート / 第三者悪口 / 性的 / 機密M&A・案件 /
   相談・面談・1on1・内部通報 ★§1.9(k)) は **記録しない** (CLAUDE.md §1.9 / privacy_review と観点を整合)

# 出力 (JSON only)

```json
{{
  "updated_memory": "<frontmatter 込みの完全な memory.md 内容>",
  "changes": [
    {{"section": "Profile", "type": "add|update|archive", "summary": "<1 行>"}}
  ],
  "wiki_promotion_candidate": "<もし wiki に昇格させるべき発見があれば 1-2 行で。無ければ空文字>"
}}
```

★厳守:
- updated_memory は **完全な md ファイル** (frontmatter + 4 セクション)
- changes は 0-5 件 (本質的変更だけ)
- 個人特定リスクの高い情報を **絶対に出さない**
- 既存内容と変わらないなら updated_memory = 既存のまま、changes = []
- **date metadata なし項目は禁止** (= 「あの件」reference resolution で時系列追跡に必須)
"""


async def sleep_time_run(user_id: str, dry_run: bool = False) -> dict:
    """1 user の memory 再整理を実行。"""
    # 構造化ログ: 1 sleep-time run = 1 event (★2026-05-21 bot logging 構造化)
    try:
        from bot_events import bot_run_context  # type: ignore
    except Exception:
        bot_run_context = None

    history = _load_recent_history(user_id)
    user_turns = [r for r in history if r.get("role") == "user"]
    if len(user_turns) < MIN_TURNS_TO_RUN:
        logger.info(f"[{user_id[:8]}] skip: only {len(user_turns)} user turns (< {MIN_TURNS_TO_RUN})")
        # 構造化ログ: skip も記録
        try:
            from bot_events import log_bot_event  # type: ignore
            log_bot_event(
                "sleep_time",
                "turn_skipped",
                user_id=user_id[:8],
                reason="too_few_turns",
                user_turns=len(user_turns),
                threshold=MIN_TURNS_TO_RUN,
                dry_run=dry_run,
            )
        except Exception:
            pass
        return {"skipped": True, "reason": "too_few_turns"}

    existing = _load_memory(user_id)
    conv_lines = []
    for r in history:
        role = r.get("role", "?")
        text = (r.get("text") or "").replace("\n", " ")[:300]
        conv_lines.append(f"{role}: {text}")
    conversation = "\n".join(conv_lines)

    # ★Tier 1: 現在日付を prompt に inject (= date metadata 必須化、JST 基準)
    from datetime import datetime as _dt, timezone as _tz, timedelta as _td
    _JST = _tz(_td(hours=9))
    today_str = _dt.now(_JST).strftime("%Y-%m-%d")
    prompt = SLEEP_TIME_PROMPT.format(
        today=today_str,
        existing_memory=existing[:5000] or "(まだ memory 無し)",
        conversation=conversation[:8000],
    )

    # 共通: with bot_run_context があれば構造化ログ、無ければ素通り
    async def _do_run() -> dict:
        try:
            out = await call_llm(prompt, model="smart", max_tokens=4000, temperature=0.2)
        except Exception as e:
            logger.error(f"[{user_id[:8]}] LLM failed: {e}")
            return {"skipped": True, "reason": f"llm_error: {e}"}

        # JSON 抽出 (★2026-06-07 評価: privacy_review と同じく robust な extract_json に統一。
        #   失敗時は no-op = memory 保持の安全側に倒れる)
        from clone_improve_lib import extract_json  # type: ignore
        try:
            data = extract_json(out)
        except Exception as e:
            logger.warning(f"[{user_id[:8]}] no JSON / parse failed: {e}")
            return {"skipped": True, "reason": "parse_failed"}
        if not isinstance(data, dict):
            return {"skipped": True, "reason": "parse_failed"}

        updated = data.get("updated_memory", "")
        changes = data.get("changes", [])
        promo = data.get("wiki_promotion_candidate", "")

        # ★2026-06-10: lost update 防止。LLM 処理中 (数秒) に clone_memory.save が割り込んで
        #   memory を更新していないか、_save 直前に再 load して確認する。async 単一プロセスゆえ
        #   flock は event loop デッドロックを招くため、optimistic re-load check を採用
        #   (再 load〜save は await を挟まない同期処理なので割り込まれない)。
        current = _load_memory(user_id)
        if current != existing:
            logger.warning(f"[{user_id[:8]}] memory が sleep 処理中に変化 → 上書き skip (lost update 防止)")
            if not dry_run and updated:
                _save_shrink_draft(user_id, current, updated)  # LLM 提案を draft 退避 (海山確認用)
            return {"skipped": True, "reason": "concurrent_update"}

        accept, reason = _should_accept_update(existing, updated)
        if accept:
            if dry_run:
                logger.info(f"[{user_id[:8]}] [DRY] would update memory ({len(changes)} changes)")
            else:
                _backup_memory(user_id, existing)  # ★上書き前に 1 世代 backup (復元可能に)
                _save_memory(user_id, updated)
                logger.info(f"[{user_id[:8]}] memory updated ({len(changes)} changes, {len(existing)}->{len(updated)}字)")
        elif reason.startswith("shrink_guard"):
            # ★2026-06-07 評価: memory 激減 → 上書きせず drafts 退避 + 警報 (記憶の大量喪失を防ぐ)
            if not dry_run:
                _save_shrink_draft(user_id, existing, updated)
                try:
                    line_push_digest(f"⚠️ sleep_time: {user_id[:8]} の memory が {reason} → 上書き保留、drafts で確認を", "memory整理")
                except Exception:
                    pass
            logger.warning(f"[{user_id[:8]}] {reason} -> 上書きせず退避")
        else:
            logger.info(f"[{user_id[:8]}] no change ({reason})")

        # 昇格候補があれば drafts に出す
        if promo and promo.strip() and not dry_run:
            DRAFTS_DIR.mkdir(parents=True, exist_ok=True)
            from datetime import datetime, timezone, timedelta
            JST = timezone(timedelta(hours=9))
            ts = datetime.now(JST).strftime("%Y-%m-%d_%H%M")
            draft = DRAFTS_DIR / f"{ts}-{user_id[:8]}.md"
            draft.write_text(
                f"# sleep-time promotion candidate ({user_id[:12]})\n\n{promo}\n\n## 根拠\n\n"
                + "\n".join(f"- {c.get('section')}: {c.get('summary', '')}" for c in changes),
                encoding="utf-8",
            )
            logger.info(f"[{user_id[:8]}] promo candidate → {draft.name}")

        return {
            "user_id_prefix": user_id[:8],
            "n_changes": len(changes),
            "changes": changes,
            "has_promotion": bool(promo and promo.strip()),
        }

    if bot_run_context is not None:
        with bot_run_context(
            "sleep_time",
            user_id=user_id[:8],
            user_turns=len(user_turns),
            existing_chars=len(existing),
            dry_run=dry_run,
        ) as ctx:
            result = await _do_run()
            # ctx に結果サマリを残す
            ctx["n_changes"] = result.get("n_changes", 0)
            ctx["has_promotion"] = result.get("has_promotion", False)
            ctx["skipped"] = result.get("skipped", False)
            if result.get("reason"):
                ctx["reason"] = result["reason"]
            return result
    return await _do_run()


async def schedule_sleep_time_agent(
    user_id: str,
    debounce_sec: int = DEFAULT_DEBOUNCE_SEC,
) -> None:
    """既存 idle task があれば cancel して新しい debounce timer を起動。

    main.py の webhook handler から各ターン後に呼ばれる:
        asyncio.create_task(schedule_sleep_time_agent(user_id))

    動作:
    - 連続会話中 (30 秒以内に次のターン) → cancel される (タスクの寿命だけが延びる)
    - 会話一区切り (30 秒経過) → sleep_time_run() が走る
    """
    # 既存 task を cancel
    existing = _idle_tasks.get(user_id)
    if existing and not existing.done():
        existing.cancel()
        logger.debug(f"[{user_id[:8]}] cancel previous idle task")

    async def _wait_and_run():
        try:
            await asyncio.sleep(debounce_sec)
            # ここまで来たら idle が成立、memory 再整理
            await sleep_time_run(user_id)
        except asyncio.CancelledError:
            # 次のターンで cancel された (正常)
            pass
        except Exception as e:
            logger.exception(f"[{user_id[:8]}] sleep_time_run error: {e}")
        finally:
            # 自分自身を dict から外す (完了 or cancel)
            if _idle_tasks.get(user_id) is task:
                _idle_tasks.pop(user_id, None)

    task = asyncio.create_task(_wait_and_run())
    _idle_tasks[user_id] = task


# ─── CLI (standalone 実行用) ─────────────────────────────────────
async def _cli_main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--user-id", required=True)
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    result = await sleep_time_run(args.user_id, dry_run=args.dry_run)
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    asyncio.run(_cli_main())
