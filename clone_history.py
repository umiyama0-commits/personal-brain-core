"""
うみやまAI 1:1 会話履歴ストア
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

LINE Works で社員が うみやまAI に送った DM の履歴を保存・参照する。

保存形式: JSONL (1 行 = 1 メッセージ)
場所:   data/brain/clone_history/<user_id>.jsonl
権限:   海山さん (個人LINE Bot /clone-log コマンド) のみ閲覧可

Record 形式:
  {
    "timestamp": "2026-04-23T12:34:56+09:00",
    "user_id": "xxxxx",
    "user_display": "田中太郎",  # 取得できれば
    "channel_id": null | "xxxxx",  # ★2026-05-24 Tier 0 追加: group メッセージの場合のみ
    "role": "user" | "assistant",
    "text": "質問 or 回答本文"
  }

# ★2026-05-24 channel_id 追加 (LINE WORKS group 対応 Tier 0)
- channel_id = null: 1:1 DM (= 既存挙動と同じ、後方互換)
- channel_id = "...": LINE WORKS group/channel 内発言
- 既存 record は channel_id フィールドが無い → load 時に null として扱う
- load_recent に optional channel_id filter 追加 (= group のみ / DM のみ 取得可)
"""
from __future__ import annotations

import os
import json
import logging
from datetime import datetime
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)

BRAIN_ROOT = Path(os.getenv("BRAIN_ROOT", "/app/data/brain"))
HISTORY_DIR = BRAIN_ROOT / "clone_history"


def _ensure_dir():
    HISTORY_DIR.mkdir(parents=True, exist_ok=True)


def _user_file(user_id: str) -> Path:
    # user_id は LINE Works 側で英数 + 記号の長い文字列。スラッシュを無効化
    safe = user_id.replace("/", "_").replace("..", "_")
    return HISTORY_DIR / f"{safe}.jsonl"


def append(
    user_id: str,
    role: str,
    text: str,
    user_display: Optional[str] = None,
    channel_id: Optional[str] = None,
) -> None:
    """1 メッセージを append.

    ★2026-05-24 channel_id 追加 (Tier 0 LINE WORKS group 対応):
    - channel_id = None: 1:1 DM (= 既存挙動と同じ、後方互換)
    - channel_id = "...": group/channel 内発言
    """
    if role not in ("user", "assistant"):
        raise ValueError(f"role must be user|assistant, got {role!r}")
    _ensure_dir()
    record = {
        "timestamp": datetime.now().astimezone().isoformat(timespec="seconds"),
        "user_id": user_id,
        "user_display": user_display,
        "channel_id": channel_id,  # ★Tier 0 group 対応 (None = DM, str = group)
        "role": role,
        "text": text,
    }
    path = _user_file(user_id)
    # ★fix 2026-05-25 MUST-FIX M-7: 200 人同時 DM で同一 user 並行 append が
    # 起きると "a" mode でも 部分書き出し → JSON decode 失敗で履歴破壊。
    # fcntl.flock で advisory lock (同一 user_id file 内 serialized)。
    # macOS / Linux で動作、Windows では fcntl 無いので fallback (= lock 無し)。
    try:
        import fcntl  # POSIX 専用
        line = json.dumps(record, ensure_ascii=False) + "\n"
        with path.open("a", encoding="utf-8") as f:
            fcntl.flock(f.fileno(), fcntl.LOCK_EX)
            try:
                f.write(line)
                f.flush()
            finally:
                fcntl.flock(f.fileno(), fcntl.LOCK_UN)
    except ImportError:
        # Windows etc. — fcntl 無し、lock 無しで append (race 可能性残るが Mac/Linux 本番想定)
        try:
            with path.open("a", encoding="utf-8") as f:
                f.write(json.dumps(record, ensure_ascii=False) + "\n")
        except Exception as e:
            logger.exception(f"clone_history.append (no-lock) failed: {e}")
    except Exception as e:
        logger.exception(f"clone_history.append failed: {e}")


def load_recent(
    user_id: str,
    n: int = 20,
    channel_id: Optional[str] = None,
    scope: str = "any",
) -> list[dict]:
    """最新 n 件を読み、role/content 形式で返す (LLM messages 用).

    ★2026-05-24 channel_id filter 追加 (Tier 0 group 対応):
    - scope="any" (default): channel_id 関係なく全件 (= 既存挙動、後方互換)
    - scope="dm": channel_id が None の record のみ (= DM のみ)
    - scope="channel": channel_id が指定値の record のみ (= 該当 group のみ)
                       (この場合 channel_id 引数必須)

    既存 record (= channel_id field 未保存) は scope=="dm" で hit、scope=="channel" で miss。
    """
    if scope == "channel" and not channel_id:
        raise ValueError("scope='channel' requires channel_id arg")

    path = _user_file(user_id)
    if not path.exists():
        return []
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except Exception:
        return []

    # filter scope に応じて先に絞る (= n 件抽出は filter 後)
    filtered = []
    for line in lines:
        line = line.strip()
        if not line:
            continue
        try:
            r = json.loads(line)
        except Exception:
            continue
        rec_ch = r.get("channel_id")  # 既存 record は None
        if scope == "dm" and rec_ch is not None:
            continue
        if scope == "channel" and rec_ch != channel_id:
            continue
        filtered.append(r)

    records = []
    for r in filtered[-n:]:
        records.append({"role": r["role"], "content": r["text"]})
    return records


def list_users() -> list[dict]:
    """全ユーザのサマリ (海山 /clone-log で一覧表示用)"""
    _ensure_dir()
    summaries = []
    for f in sorted(HISTORY_DIR.glob("*.jsonl")):
        try:
            lines = f.read_text(encoding="utf-8").splitlines()
        except Exception:
            continue
        if not lines:
            continue
        # 最終メッセージから display_name を取得
        last_display = None
        for line in reversed(lines):
            try:
                r = json.loads(line)
                if r.get("user_display"):
                    last_display = r["user_display"]
                    break
            except Exception:
                continue
        summaries.append({
            "user_id": f.stem,
            "display": last_display,
            "message_count": len(lines),
            "last_updated": f.stat().st_mtime,
        })
    summaries.sort(key=lambda x: x["last_updated"], reverse=True)
    return summaries


def dump_user(user_id: str, n: int = 50) -> str:
    """ユーザの履歴を Markdown で整形 (海山 /clone-log <user> で表示用)"""
    path = _user_file(user_id)
    if not path.exists():
        return f"履歴なし: {user_id}"
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except Exception:
        return f"履歴読込エラー: {user_id}"

    out = [f"# 履歴: {user_id}", f"総件数: {len(lines)}", ""]
    for line in lines[-n:]:
        try:
            r = json.loads(line)
            ts = r.get("timestamp", "")[:19]
            role = "👤" if r["role"] == "user" else "🤖"
            text = r.get("text", "").replace("\n", " ")[:200]
            # ★2026-05-24 channel marker (Tier 0): group 内発言は [G:xxxxxxxx] prefix
            ch = r.get("channel_id")
            ch_marker = f" [G:{ch[:8]}]" if ch else ""
            out.append(f"{ts} {role}{ch_marker} {text}")
        except Exception:
            continue
    return "\n".join(out)


def forget(user_id: str) -> bool:
    """指定ユーザの履歴を削除 (/clone-forget)"""
    path = _user_file(user_id)
    if path.exists():
        path.unlink()
        return True
    return False
