"""
tasks/self_improve.py — 自己改善ループ (★2026-05-22 Phase 4 切り出し)

main.py の以下 3 つを移管:
- _read_last_self_improve_ts / _write_last_self_improve_ts (state file 永続化)
- _self_improve_loop (6h ごとの run_self_improve 実行 + LINE Push)

設計:
- main.py からの循環 import を避けるため、push_message は callable で受ける (DI パターン)
- LITELLM_URL / LITELLM_KEY / ALIGNMENT_TARGET_USER は env から直接読む (main.py と同じ)
"""
from __future__ import annotations

import asyncio
import logging
import os
import time as _time_mod
from pathlib import Path
from typing import Awaitable, Callable, Optional

from self_improve import run_self_improve

logger = logging.getLogger(__name__)

# state file: 前回実行時刻を永続化 (= 再起動による連射防止)
SELF_IMPROVE_INTERVAL_SEC = int(os.getenv("SELF_IMPROVE_INTERVAL_SEC", str(6 * 3600)))
SELF_IMPROVE_STATE_FILE = Path(
    os.getenv("SELF_IMPROVE_STATE_FILE", "/app/data/brain/self_improve_last_run.txt")
)

# main.py 由来 env / config (引数 DI でなく env 直読みで OK、起動時に固定)
LITELLM_URL = os.getenv("LITELLM_URL", "http://litellm:4000")
LITELLM_KEY = os.getenv("LITELLM_MASTER_KEY", "")  # ★平文 default 禁止 (社内レビュー §3.1)
ALIGNMENT_TARGET_USER = os.getenv("ALIGNMENT_TARGET_USER", "")


def read_last_self_improve_ts() -> float:
    """state file から前回実行 UNIX 時刻を読む。未存在なら 0.0。"""
    try:
        return float(SELF_IMPROVE_STATE_FILE.read_text().strip())
    except Exception:
        return 0.0


def write_last_self_improve_ts(ts: float) -> None:
    """state file に UNIX 時刻を書き込む。"""
    try:
        SELF_IMPROVE_STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
        SELF_IMPROVE_STATE_FILE.write_text(str(ts))
    except Exception as e:
        logger.warning(f"self_improve state persist error: {e}")


# push_message の callable type (= main.py の push_message 関数を DI で受ける)
PushMessageFn = Callable[..., Awaitable[None]]


async def self_improve_loop(
    app,
    push_message_fn: Optional[PushMessageFn] = None,
):
    """SELF_IMPROVE_INTERVAL_SEC ごとに会話ログを分析し、自己改善を実行。

    ★クラッシュ再起動による連射対策:
       前回実行時刻を永続化し、再起動直後でも残り時間だけ待機する。
       これにより、コンテナ再起動のたびに 5分後に走る挙動を防ぐ。

    Args:
        app: FastAPI app (app.state.redis / app.state.http が必要)
        push_message_fn: 通知用 callable (= main.py の push_message を DI)。
                         未指定なら通知 skip (= state file 更新のみ走る)
    """
    # 再起動ループ防止: 前回からの経過時間を見る
    now = _time_mod.time()
    last = read_last_self_improve_ts()
    elapsed = now - last
    min_warmup = 300  # 通常起動時の 5 分ウォームアップ
    if last > 0 and elapsed < SELF_IMPROVE_INTERVAL_SEC:
        remaining = SELF_IMPROVE_INTERVAL_SEC - elapsed
        # ウォームアップ ≤ 残り時間 なら残り時間だけ待つ
        wait = max(min_warmup, remaining)
        logger.info(
            f"self_improve: last run {int(elapsed)}s ago — "
            f"next in {int(wait)}s (interval={SELF_IMPROVE_INTERVAL_SEC}s)"
        )
        await asyncio.sleep(wait)
    else:
        await asyncio.sleep(min_warmup)

    while True:
        try:
            result = await run_self_improve(
                app.state.redis,
                app.state.http,
                LITELLM_URL,
                LITELLM_KEY,
            )
            write_last_self_improve_ts(_time_mod.time())
            logger.info(f"自己改善ループ結果: {result}")

            # 改善があった場合、CEO に通知
            if "適用" in result and ALIGNMENT_TARGET_USER and push_message_fn:
                await push_message_fn(
                    app.state.http,
                    ALIGNMENT_TARGET_USER,
                    f"🔧 自己改善レポート\n{result}",
                )
        except Exception as e:
            logger.warning(f"Self-improve loop error: {e}")

        await asyncio.sleep(SELF_IMPROVE_INTERVAL_SEC)
