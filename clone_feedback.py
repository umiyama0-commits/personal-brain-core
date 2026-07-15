"""
うみやまAI フィードバック / 訂正ストア
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

社員からの「修正希望」を収集し、海山がレビューして Wiki 反映を判断するためのキュー。

フロー:
  1. 社員が うみやまAI の応答に「✏️ 修正希望あり」をタップ or /fix と返信
  2. bot: 「直前の応答のどこが違ったか、次のメッセージで具体的に」
  3. 社員: 修正内容を送信
  4. bot: 保存 + サンクス
  5. 海山 LINE で /clone-feedback でレビュー、/clone-feedback-accept で Wiki へ

保存:
  data/brain/clone_feedback/YYYY-MM-DD.jsonl    — 日別のフィードバック記録
  data/brain/clone_feedback/.awaiting.json      — 次メッセージ待ち状態 (user_id → {since, last_response})

Record 形式:
  {
    "id": "2026-04-24_001",
    "timestamp": "2026-04-24T12:34:56+09:00",
    "user_id": "xxx",
    "user_display": "田中太郎",
    "trigger_msg": "今期の戦略は？",         # 応答を引き出したユーザ発言
    "response": "FY27 の戦略は...",          # うみやまAI の応答 (修正対象)
    "feedback": "売上数字が古いです。最新は XXX",  # 修正内容
    "status": "pending"                       # pending | accepted | rejected | noted
  }
"""
from __future__ import annotations

import os
import json
import uuid
import logging
from datetime import datetime, timedelta
from pathlib import Path
from typing import Optional

from services._review_store import locked, append_jsonl, write_text_atomic

logger = logging.getLogger(__name__)

BRAIN_ROOT = Path(os.getenv("BRAIN_ROOT", "/app/data/brain"))
FEEDBACK_DIR = BRAIN_ROOT / "clone_feedback"
AWAITING_FILE = FEEDBACK_DIR / ".awaiting.json"

# 待ち状態の有効期限 (分)。過ぎたら自動キャンセル
AWAITING_TTL_MIN = 30


def _ensure_dir():
    FEEDBACK_DIR.mkdir(parents=True, exist_ok=True)


def _today_file() -> Path:
    return FEEDBACK_DIR / f"{datetime.now().strftime('%Y-%m-%d')}.jsonl"


def _load_awaiting() -> dict:
    if not AWAITING_FILE.exists():
        return {}
    try:
        return json.loads(AWAITING_FILE.read_text(encoding="utf-8"))
    except Exception:
        return {}


def _save_awaiting(d: dict):
    _ensure_dir()
    write_text_atomic(AWAITING_FILE, json.dumps(d, ensure_ascii=False, indent=2))


# ─── 待ち状態管理 ──────────────────────────────────
def start_awaiting(user_id: str, trigger_msg: str, response: str, user_display: Optional[str] = None) -> None:
    """ユーザを『修正内容待ち』状態にする"""
    _ensure_dir()
    with locked(AWAITING_FILE):
        d = _load_awaiting()
        d[user_id] = {
            "since": datetime.now().astimezone().isoformat(timespec="seconds"),
            "trigger_msg": trigger_msg,
            "response": response,
            "user_display": user_display,
        }
        _save_awaiting(d)


def is_awaiting(user_id: str) -> bool:
    """現在『修正内容待ち』かチェック (TTL 超過は False 扱い + クリア)"""
    _ensure_dir()
    with locked(AWAITING_FILE):
        d = _load_awaiting()
        info = d.get(user_id)
        if not info:
            return False
        try:
            since = datetime.fromisoformat(info["since"])
            if datetime.now().astimezone() - since > timedelta(minutes=AWAITING_TTL_MIN):
                # TTL 切れ → クリア
                d.pop(user_id, None)
                _save_awaiting(d)
                return False
        except Exception:
            pass
        return True


def get_awaiting_context(user_id: str) -> Optional[dict]:
    return _load_awaiting().get(user_id)


def cancel(user_id: str) -> bool:
    """待ち状態をクリア"""
    _ensure_dir()
    with locked(AWAITING_FILE):
        d = _load_awaiting()
        if user_id in d:
            d.pop(user_id)
            _save_awaiting(d)
            return True
        return False


# ─── フィードバック保存 ────────────────────────────
def save_feedback(
    user_id: str,
    feedback_text: str,
    user_display: Optional[str] = None,
) -> Optional[dict]:
    """待ち状態の context と合わせてフィードバックを保存。context なければ None"""
    ctx = get_awaiting_context(user_id)
    if not ctx:
        return None
    _ensure_dir()
    record = {
        "id": f"{datetime.now().strftime('%Y-%m-%d')}_{uuid.uuid4().hex[:6]}",
        "timestamp": datetime.now().astimezone().isoformat(timespec="seconds"),
        "user_id": user_id,
        "user_display": user_display or ctx.get("user_display"),
        "trigger_msg": ctx.get("trigger_msg", ""),
        "response": ctx.get("response", ""),
        "feedback": feedback_text,
        "status": "pending",
    }
    path = _today_file()
    with locked(path):
        append_jsonl(path, record)
    # 待ち状態クリア
    cancel(user_id)
    logger.info(f"clone_feedback saved id={record['id']} user={user_id}")
    return record


# ─── 👍👎 rating (★2026-07-10 世界基準評価 S3、利用者信号の再生) ─────────
# 修正待ち (.awaiting.json) とは別 store。ratings は「回答の良し悪しの 1タップ signal」で
# 訂正テキストとは別次元。品質トリアージ (negative rating → 該当応答を review) の入口。
RATING_AWAITING_FILE = FEEDBACK_DIR / ".rating_awaiting.json"


def _load_rating_awaiting() -> dict:
    if not RATING_AWAITING_FILE.exists():
        return {}
    try:
        return json.loads(RATING_AWAITING_FILE.read_text(encoding="utf-8"))
    except Exception:
        return {}


def _save_rating_awaiting(d: dict):
    _ensure_dir()
    write_text_atomic(RATING_AWAITING_FILE, json.dumps(d, ensure_ascii=False, indent=2))


def start_rating(user_id: str, trigger_msg: str, response: str,
                 user_display: Optional[str] = None) -> None:
    """👍👎 prompt 送信時に response context を pending 保存 (postback で紐付ける)。"""
    _ensure_dir()
    with locked(RATING_AWAITING_FILE):
        d = _load_rating_awaiting()
        d[user_id] = {
            "since": datetime.now().astimezone().isoformat(timespec="seconds"),
            "trigger_msg": (trigger_msg or "")[:400],
            "response": (response or "")[:1200],
            "user_display": user_display,
        }
        _save_rating_awaiting(d)


def save_rating(user_id: str, rating: str,
                user_display: Optional[str] = None) -> Optional[dict]:
    """postback (clonefb=good/bad) を rating record として保存。context は pending から復元。"""
    if rating not in ("good", "bad"):
        return None
    _ensure_dir()
    with locked(RATING_AWAITING_FILE):
        d = _load_rating_awaiting()
        ctx = d.pop(user_id, None)
        _save_rating_awaiting(d)
    ctx = ctx or {}
    record = {
        "id": f"{datetime.now().strftime('%Y-%m-%d')}_r{uuid.uuid4().hex[:6]}",
        "timestamp": datetime.now().astimezone().isoformat(timespec="seconds"),
        "user_id": user_id,
        "user_display": user_display or ctx.get("user_display"),
        "trigger_msg": ctx.get("trigger_msg", ""),
        "response": ctx.get("response", ""),
        "kind": "rating",
        "rating": rating,
        "reason": None,   # ★2026-07-11: 👎 の 1タップ理由 (num/nodata/offtopic/style) を後追いで attach
        "feedback": "👍 役立った" if rating == "good" else "👎 いまいち",
        "status": "rating",
    }
    path = _today_file()
    with locked(path):
        append_jsonl(path, record)
    logger.info(f"clone_feedback rating saved id={record['id']} user={user_id} rating={rating}")
    return record


# ★2026-07-11 採用レビュー #3: 👎 の 1タップ理由を「直前の bad record」に後追い付与する。
#   bad は tap 時点で即保存済み (signal を失わない) → 理由 tap で同 record を enrich。
_REASON_LABELS = {
    "num": "数字が違う", "nodata": "情報がない",
    "offtopic": "質問に答えてない", "style": "言い方・文体",
}


def attach_reason(user_id: str, reason_code: str) -> bool:
    """user の当日最新 bad rating (reason 未設定) に理由を付与。today file の該当行を書換。"""
    if reason_code not in _REASON_LABELS:
        return False
    path = _today_file()
    with locked(path):
        if not path.exists():
            return False
        lines = path.read_text(encoding="utf-8").splitlines()
        # 新しい順に走査し、最初の「該当 user の bad で reason 未設定」を更新
        for i in range(len(lines) - 1, -1, -1):
            s = lines[i].strip()
            if not s:
                continue
            try:
                rec = json.loads(s)
            except Exception:
                continue
            if (rec.get("kind") == "rating" and rec.get("user_id") == user_id
                    and rec.get("rating") == "bad" and not rec.get("reason")):
                rec["reason"] = reason_code
                rec["reason_label"] = _REASON_LABELS[reason_code]
                lines[i] = json.dumps(rec, ensure_ascii=False)
                write_text_atomic(path, "\n".join(lines) + "\n")
                logger.info(f"clone_feedback reason attached user={user_id} reason={reason_code}")
                return True
    return False


def aggregate_ratings(days: int = 7) -> dict:
    """直近 days 日の 👍👎 rating を集計 (週次レポート/トリアージ用、決定論)。
    return: {good, bad, total, reason_counts: {code: n}, recent_bad: [{trigger, response, reason_label}]}"""
    from datetime import timedelta
    cutoff = (datetime.now().astimezone() - timedelta(days=days))
    good = bad = 0
    reason_counts: dict = {}
    recent_bad: list = []
    for rec in _iter_all_records():
        if rec.get("kind") != "rating":
            continue
        ts = rec.get("timestamp", "")
        try:
            if datetime.fromisoformat(ts) < cutoff:
                continue
        except Exception:
            pass
        if rec.get("rating") == "good":
            good += 1
        elif rec.get("rating") == "bad":
            bad += 1
            rc = rec.get("reason")
            if rc:
                reason_counts[rc] = reason_counts.get(rc, 0) + 1
            if len(recent_bad) < 5:
                recent_bad.append({
                    "trigger": (rec.get("trigger_msg") or "")[:60],
                    "response": (rec.get("response") or "")[:80],
                    "reason_label": rec.get("reason_label") or "(理由未選択)",
                })
    return {"good": good, "bad": bad, "total": good + bad,
            "reason_counts": reason_counts, "recent_bad": recent_bad}


# ─── レビュー用 (/clone-feedback) ───────────────────
def _iter_all_records():
    """全日付分を新しい順に yield"""
    if not FEEDBACK_DIR.exists():
        return
    files = sorted(FEEDBACK_DIR.glob("*.jsonl"), reverse=True)
    for f in files:
        try:
            lines = f.read_text(encoding="utf-8").splitlines()
        except Exception:
            continue
        for line in reversed(lines):
            line = line.strip()
            if not line:
                continue
            try:
                yield json.loads(line)
            except Exception:
                continue


def list_pending(limit: int = 20) -> list[dict]:
    """pending なものを新しい順"""
    out = []
    for r in _iter_all_records():
        if r.get("status") == "pending":
            out.append(r)
            if len(out) >= limit:
                break
    return out


def list_all(limit: int = 50) -> list[dict]:
    out = []
    for r in _iter_all_records():
        out.append(r)
        if len(out) >= limit:
            break
    return out


def find_by_id(fid: str) -> Optional[dict]:
    for r in _iter_all_records():
        if r.get("id") == fid:
            return r
    return None


def update_status(fid: str, new_status: str) -> bool:
    """該当レコードの status を書き換え (全行再書き込み)"""
    if new_status not in ("pending", "accepted", "rejected", "noted"):
        raise ValueError(new_status)
    return _update_record(fid, {"status": new_status})


def attach_backcheck(fid: str, backcheck: dict) -> bool:
    """バックチェック結果をレコードに付与"""
    return _update_record(fid, {"backcheck": backcheck})


# ─── ★2026-05-12: コメント機能 (海山がダイジェスト経由でコメント追加) ───
# コメント待機状態は別 file `.awaiting_comment.json` で管理 (修正希望本文待機の AWAITING_FILE とは別)
AWAITING_COMMENT_FILE = FEEDBACK_DIR / ".awaiting_comment.json"


def _load_awaiting_comment() -> dict:
    if not AWAITING_COMMENT_FILE.exists():
        return {}
    try:
        return json.loads(AWAITING_COMMENT_FILE.read_text(encoding="utf-8"))
    except Exception:
        return {}


def _save_awaiting_comment(d: dict):
    _ensure_dir()
    write_text_atomic(
        AWAITING_COMMENT_FILE, json.dumps(d, ensure_ascii=False, indent=2)
    )


def start_comment_awaiting(reviewer_user_id: str, target_fid: str, kind: str = "feedback") -> None:
    """海山が「💬 コメント」ボタンを押した時に呼び出し、待機状態をセット。

    kind: "feedback" (clone_feedback の item) or "learning" (clone_learning の item)
    """
    _ensure_dir()
    with locked(AWAITING_COMMENT_FILE):
        d = _load_awaiting_comment()
        d[reviewer_user_id] = {
            "since": datetime.now().astimezone().isoformat(timespec="seconds"),
            "target_fid": target_fid,
            "kind": kind,
        }
        _save_awaiting_comment(d)


def get_comment_awaiting(reviewer_user_id: str) -> Optional[dict]:
    """待機中のコメント対象を返す (TTL 超過なら None + クリア)"""
    _ensure_dir()
    with locked(AWAITING_COMMENT_FILE):
        d = _load_awaiting_comment()
        info = d.get(reviewer_user_id)
        if not info:
            return None
        try:
            since = datetime.fromisoformat(info["since"])
            if datetime.now().astimezone() - since > timedelta(minutes=AWAITING_TTL_MIN):
                d.pop(reviewer_user_id, None)
                _save_awaiting_comment(d)
                return None
        except Exception:
            pass
        return info


def cancel_comment(reviewer_user_id: str) -> bool:
    _ensure_dir()
    with locked(AWAITING_COMMENT_FILE):
        d = _load_awaiting_comment()
        if reviewer_user_id in d:
            d.pop(reviewer_user_id)
            _save_awaiting_comment(d)
            return True
        return False


def add_comment(fid: str, comment: str, reviewer: str = "umiyama") -> bool:
    """clone_feedback レコードにコメントを追記 (既存 comments list に append)。

    comments 形式: [{"ts": ISO, "by": "umiyama", "text": "..."}]
    """
    rec = find_by_id(fid)
    if not rec:
        return False
    existing = rec.get("comments") or []
    if not isinstance(existing, list):
        existing = []
    existing.append({
        "ts": datetime.now().astimezone().isoformat(timespec="seconds"),
        "by": reviewer,
        "text": comment,
    })
    return _update_record(fid, {"comments": existing})


def _update_record(fid: str, updates: dict) -> bool:
    """該当レコードを更新 (全行再書き込み)"""
    for f in sorted(FEEDBACK_DIR.glob("*.jsonl")):
        with locked(f):
            try:
                lines = f.read_text(encoding="utf-8").splitlines()
            except Exception:
                continue
            changed = False
            new_lines = []
            for line in lines:
                line = line.strip()
                if not line:
                    continue
                try:
                    r = json.loads(line)
                    if r.get("id") == fid:
                        r.update(updates)
                        r["updated"] = datetime.now().astimezone().isoformat(timespec="seconds")
                        changed = True
                    new_lines.append(json.dumps(r, ensure_ascii=False))
                except Exception:
                    new_lines.append(line)
            if changed:
                write_text_atomic(f, "\n".join(new_lines) + "\n")
                return True
    return False


VERDICT_LABEL = {
    "supports_correction": "✅ Wiki が修正を裏付け",
    "contradicts_correction": "⚠️ Wiki と食い違い",
    "wiki_silent": "❓ Wiki 未記載 (要海山確認)",
    "ambiguous": "🤔 判断微妙",
}


def _verdict_emoji(r: dict) -> str:
    bc = r.get("backcheck") or {}
    v = bc.get("verdict")
    if v == "supports_correction":
        return "✅"
    if v == "contradicts_correction":
        return "⚠️"
    if v == "wiki_silent":
        return "❓"
    if v == "ambiguous":
        return "🤔"
    return "⏳"  # backcheck 未完


# ─── 表示フォーマット (海山 LINE Bot 用) ─────────────
def summary(limit: int = 10) -> str:
    pending = list_pending(limit)
    if not pending:
        return "未レビューの修正希望はありません 👍"
    lines = [f"# 未レビュー修正希望 ({len(pending)}件)", ""]
    for r in pending:
        display = r.get("user_display") or r["user_id"][:8]
        ts = r["timestamp"][:16].replace("T", " ")
        emoji = _verdict_emoji(r)
        lines.append(f"{emoji} `{r['id']}`  {ts}  {display}")
        lines.append(f"  Q: {r.get('trigger_msg','')[:50]}")
        lines.append(f"  🤖 {r.get('response','')[:60]}")
        lines.append(f"  ✏️ {r.get('feedback','')[:100]}")
        bc = r.get("backcheck") or {}
        if bc.get("summary"):
            # 1行目のみ
            first_line = bc["summary"].split("\n")[0][:100]
            lines.append(f"  📚 {first_line}")
        lines.append("")
    lines.append("凡例: ✅裏付け / ⚠️矛盾 / ❓未記載 / 🤔微妙 / ⏳検証中")
    lines.append("詳細: /clone-feedback <id>")
    lines.append("取込: /clone-feedback-accept <id>")
    lines.append("見送: /clone-feedback-reject <id>")
    return "\n".join(lines)


def detail(fid: str) -> str:
    r = find_by_id(fid)
    if not r:
        return f"見つかりません: {fid}"
    display = r.get("user_display") or r["user_id"][:12]
    lines = [
        f"# Feedback {r['id']}",
        f"📅 {r['timestamp']}",
        f"👤 {display}",
        f"🏷  status: {r.get('status')}",
        "",
        "## 発言 (社員)",
        r.get("trigger_msg", ""),
        "",
        "## うみやまAI 応答",
        r.get("response", ""),
        "",
        "## 修正希望内容",
        r.get("feedback", ""),
    ]
    bc = r.get("backcheck") or {}
    if bc:
        verdict = bc.get("verdict", "")
        label = VERDICT_LABEL.get(verdict, verdict)
        lines += [
            "",
            "## バックチェック結果",
            f"判定: {label}",
            "",
            bc.get("summary", ""),
        ]
        srcs = bc.get("sources") or []
        if srcs:
            lines.append("")
            lines.append("参照:")
            for s in srcs:
                lines.append(f"  - {s}")
    else:
        lines += ["", "## バックチェック結果", "⏳ 未実行 or 実行中"]
    return "\n".join(lines)
