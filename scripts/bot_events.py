"""
bot_events.py — bot 応答系の構造化ログ (★2026-05-21 追加)

目的:
  既存の `scripts/extractors/_common.log_event` は extractor (nightly cron 系)
  用の events.jsonl だが、本ファイルは **bot turn (応答ターン) 用**。
  全 bot turn を 1 行 JSON にすると、後で grep/jq で:
    - p50/p95 latency
    - 失敗率 (model / component 別)
    - heavy user / 時間帯別 traffic
    - turn 内訳 (memory_update / sleep_time / respond)
  が追加コード無しで集計できる。

設計:
  - スタンドアロン (重い依存ナシ、import が安全)。main.py / clone_memory.py /
    sleep_time_agent / clone_respond_public 等から軽量に呼び出せる
  - 出力先: data/brain/bot_events/events.jsonl (extractor の events.jsonl と分離)
  - 旧来の `logger.info(...)` は併存 (構造化ログは検索性、人間用は logger に残す)
  - log 書き込み失敗で bot 自体は止めない (warning に逃がす)

API:
  log_bot_event("clone_respond", "turn_started", user_id="...", model="smart")
  with bot_run_context("clone_respond", user_id="...", model="smart") as ctx:
      ctx["response_chars"] = len(response)
      ctx["context_chars"] = len(prompt)
"""
from __future__ import annotations

import json
import logging
import os
import time
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path
from typing import Any

logger = logging.getLogger("bot_events")

# 出力先 (BRAIN_ROOT / "bot_events" / "events.jsonl")
# - BRAIN_ROOT を尊重 (docker 内 /app/data/brain / 開発時 BRAIN_ROOT 上書き対応)
_DEFAULT_BRAIN_ROOT = "/app/data/brain"


def _events_log_path() -> Path:
    """毎回環境変数を読む (test 環境で monkeypatch される場合に対応)。

    ★2026-06-07 (エージェント評価 C1): BRAIN_ROOT 未設定時は BRAIN_APP_ROOT/data/brain に fallback。
    host cron は cron_env.sh で BRAIN_APP_ROOT のみ export し BRAIN_ROOT を export しないため、
    既定 /app/data/brain だと host 上で events.jsonl を空振り → 監視 (uptime/monitor_daily/cost) が
    盲目化する。container は両 env 未設定 → /app/data/brain (正)、host cron は BRAIN_APP_ROOT 経由で
    /Users/brain/brain-agent/data/brain (= bind mount 元、同一 file) を指す。
    """
    root = os.getenv("BRAIN_ROOT")
    if not root:
        app_root = os.getenv("BRAIN_APP_ROOT")
        root = f"{app_root}/data/brain" if app_root else _DEFAULT_BRAIN_ROOT
    return Path(root) / "bot_events" / "events.jsonl"


def _ensure_log_dir() -> Path:
    """events.jsonl の親ディレクトリを保証して path を返す。"""
    p = _events_log_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    return p


def log_bot_event(component: str, event: str, **fields: Any) -> None:
    """bot_events.jsonl に 1 行 JSON で追記。

    Args:
        component: clone_respond / clone_memory / sleep_time / handle_message 等
        event: turn_started / turn_finished / turn_failed / step_X 等
        **fields: 任意のメタデータ (user_id, model, latency_ms, error_msg 等)

    fields はそのまま JSON 化される。
      例: log_bot_event("clone_respond", "turn_finished",
                        user_id="abc", model="smart", latency_ms=8200,
                        response_chars=420, context_chars=48000)
      後で `grep '"component":"clone_respond"' events.jsonl | jq '.latency_ms'`
      で p50/p95 が取れる。

    失敗時 (ディスクフル等) は warning に逃がす。bot 本体を止めない。
    """
    record = {
        "ts": datetime.now().isoformat(timespec="seconds"),
        "component": component,
        "event": event,
        **fields,
    }
    try:
        path = _ensure_log_dir()
        with path.open("a", encoding="utf-8") as f:
            f.write(json.dumps(record, ensure_ascii=False, default=str) + "\n")
    except Exception as e:
        logger.warning(f"log_bot_event failed ({component}/{event}): {e}")


@contextmanager
def bot_run_context(component: str, **start_fields: Any):
    """1 turn の start/finish/failed を自動で events.jsonl に記録する with ブロック。

    使い方:
        with bot_run_context("clone_respond", user_id=uid, model="smart") as ctx:
            response = await respond(...)
            ctx["response_chars"] = len(response)
            ctx["context_chars"] = len(prompt)
        # → turn_started + turn_finished (elapsed_sec, response_chars, context_chars 付き)

    例外時は turn_failed を出してから再 raise。
    ctx に追加したキーは turn_finished の fields に含まれる。
    """
    started = time.time()
    log_bot_event(component, "turn_started", **start_fields)
    ctx: dict[str, Any] = {}
    try:
        yield ctx
    except Exception as e:
        elapsed_ms = int((time.time() - started) * 1000)
        # ★2026-06-12 fix: f(**a, **b) は同名キーで TypeError (multiple values)。
        # COST_TRACKING_ENABLED=1 で ctx["model"]=実モデル名 が start_fields の model と
        # 衝突し、全 smart 応答が turn 終了時に爆死→「お休み」fallback になっていた
        # (5/27 実装時から潜伏、6/11 16:02 の flag 有効化で発火)。dict マージは
        # 同名キーを後勝ちで解決する (= ctx 優先、docstring の意図通り)。
        merged = {
            "elapsed_ms": elapsed_ms,
            "error_class": type(e).__name__,
            "error_msg": str(e)[:500],
            **start_fields,
            **ctx,
        }
        log_bot_event(component, "turn_failed", **merged)
        raise
    else:
        elapsed_ms = int((time.time() - started) * 1000)
        merged = {"elapsed_ms": elapsed_ms, **start_fields, **ctx}
        log_bot_event(component, "turn_finished", **merged)


# ─── 集計補助 (CLI 用) ────────────────────────
def iter_events(since_sec: int | None = None):
    """events.jsonl を 1 行ずつ dict で yield。

    since_sec: 「今から N 秒前まで遡る」フィルタ (None ならフルスキャン)。
    壊れた行は warning + skip。
    """
    path = _events_log_path()
    if not path.exists():
        return
    cutoff = time.time() - since_sec if since_sec is not None else None
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            rec = json.loads(line)
        except Exception:
            logger.warning(f"bot_events parse failed: {line[:80]}...")
            continue
        if cutoff is not None:
            try:
                ts = datetime.fromisoformat(rec["ts"]).timestamp()
                if ts < cutoff:
                    continue
            except Exception:
                pass
        yield rec


def parse_since(spec: str) -> int:
    """'7d' / '24h' / '30m' / '300s' → 秒数。"""
    if not spec:
        return 0
    s = spec.strip().lower()
    if s.endswith("d"):
        return int(s[:-1]) * 86400
    if s.endswith("h"):
        return int(s[:-1]) * 3600
    if s.endswith("m"):
        return int(s[:-1]) * 60
    if s.endswith("s"):
        return int(s[:-1])
    # default: 数字なら秒
    return int(s)
