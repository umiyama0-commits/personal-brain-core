"""
うみやまAI 会話発見ストア (learning loop)
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
社員との 1:1 DM ログから、Wiki に未反映の「新しい気づき・事実・
判断軸・改善ポイント」を LLM に抽出させ、海山レビュー用のキューに
蓄積する。

フロー:
  1. 夜間 cron で clone_history/*.jsonl を走査
     (last_scan より後のメッセージのみ対象 = インクリメンタル)
  2. ユーザ単位で直近会話を LLM に渡し「Wiki に未反映の発見があるか」判定
  3. 発見があれば data/brain/clone_learning/YYYY-MM-DD.jsonl に保存
  4. 海山に LINE で digest 送付 (翌朝)
  5. 海山: /clone-learning <id> で確認、/clone-learning-accept <id> で /teach 提案

Record 形式:
  {
    "id": "2026-04-25_abcd12",
    "timestamp": "2026-04-25T02:00:00+09:00",
    "user_id": "xxxx",
    "user_display": "田中太郎",
    "category": "fact" | "correction" | "decision" | "style" | "other",
    "insight": "社員発 / 海山AI間で判明した新しいこと (日本語 1-3 行)",
    "proposed_wiki_patch": "追記すべき Wiki のパッチ (省略可、LLM 提案)",
    "source_snippet": "該当会話スニペット (最大 400 字)",
    "scanned_range": ["2026-04-24T12:00:00", "2026-04-24T23:00:00"],
    "status": "pending" | "accepted" | "rejected" | "noted"
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

import httpx

from services._review_store import locked, write_text_atomic, append_jsonl

logger = logging.getLogger(__name__)

BRAIN_ROOT = Path(os.getenv("BRAIN_ROOT", "/app/data/brain"))
HISTORY_DIR = BRAIN_ROOT / "clone_history"
LEARNING_DIR = BRAIN_ROOT / "clone_learning"
LAST_SCAN_FILE = LEARNING_DIR / ".last_scan.json"


def _ensure_dir():
    LEARNING_DIR.mkdir(parents=True, exist_ok=True)


def _today_file() -> Path:
    return LEARNING_DIR / f"{datetime.now().strftime('%Y-%m-%d')}.jsonl"


# ─── 前回スキャン時刻管理 ─────────────────────────────
def _load_last_scan() -> dict:
    if not LAST_SCAN_FILE.exists():
        return {}
    try:
        return json.loads(LAST_SCAN_FILE.read_text(encoding="utf-8"))
    except Exception:
        return {}


def _save_last_scan(d: dict):
    _ensure_dir()
    write_text_atomic(
        LAST_SCAN_FILE, json.dumps(d, ensure_ascii=False, indent=2)
    )


# ─── 履歴走査 ────────────────────────────────────────
def _iter_new_messages_per_user(since: Optional[str] = None) -> dict[str, list[dict]]:
    """各 user_id の新メッセージを {user_id: [records]} で返す。
    since が None なら last_scan.json の per-user 値を参照。
    """
    last_scan = _load_last_scan() if since is None else {}
    out: dict[str, list[dict]] = {}
    if not HISTORY_DIR.exists():
        return out
    for f in HISTORY_DIR.glob("*.jsonl"):
        user_id = f.stem
        last = since or last_scan.get(user_id)
        try:
            lines = f.read_text(encoding="utf-8").splitlines()
        except Exception:
            continue
        msgs = []
        for line in lines:
            line = line.strip()
            if not line:
                continue
            try:
                r = json.loads(line)
            except Exception:
                continue
            ts = r.get("timestamp", "")
            if last and ts <= last:
                continue
            msgs.append(r)
        if msgs:
            out[user_id] = msgs
    return out


def update_last_scan(scanned_user_ids: list[str]):
    """スキャン完了後、各ユーザの最新タイムスタンプを記録"""
    _ensure_dir()
    with locked(LAST_SCAN_FILE):
        d = _load_last_scan()
        for uid in scanned_user_ids:
            path = HISTORY_DIR / f"{uid}.jsonl"
            if not path.exists():
                continue
            try:
                lines = path.read_text(encoding="utf-8").splitlines()
                if lines:
                    last = json.loads(lines[-1])
                    d[uid] = last.get("timestamp", d.get(uid, ""))
            except Exception:
                pass
        _save_last_scan(d)


# ─── LLM による発見抽出 ────────────────────────────────
EXTRACT_PROMPT = """あなたは海山社長のナレッジマネージャです。
以下は社員と「うみやまAI」の 1:1 DM のやりとりです。

# 会話ログ
{conversation}

# 現在の Wiki の関連部分 (抜粋)
{wiki_snippets}

# タスク
この会話から、**Wiki にまだ反映されていない** 以下のようなものを抽出してください:
- 新しい事実・データ (OWNDAYS の業務・数字・現場状況)
- 社員からの訂正・補足
- 海山AI の応答が不十分だった点 (質問に答えられていない・情報不足)
- 会話スタイル改善のヒント (うまく答えられた / 滑った箇所)
- 今後の運用改善アイデア

# 出力 (JSON array 形式のみ、説明文なし)
[
  {{
    "category": "fact" | "correction" | "decision" | "style" | "other",
    "insight": "気づき (日本語 1-3 行)",
    "proposed_wiki_patch": "追記すべきなら Wiki ファイル名と追記内容 (省略可、空文字でも可)",
    "source_snippet": "該当会話スニペット (最大 300 字)"
  }},
  ...
]

# ガイドライン
- 既に Wiki に書かれていることは抽出しない
- 海山の判断軸・価値観と整合する範囲で
- 個別社員の人事評価・処遇に関する内容は抽出しない (category=other として "海山本人に報告推奨" でも良い)
- 発見が無ければ空配列 [] を返す
- 必ず JSON のみ返す
"""


async def extract_insights(
    http: httpx.AsyncClient,
    litellm_url: str,
    litellm_key: str,
    conversation: list[dict],
    wiki_snippets: str,
    model: str = "fast-gpt",
) -> Optional[list[dict]]:
    """会話ログ + Wiki context から発見を抽出。失敗時 None (genuine empty の [] と区別)。"""
    # 会話を整形 (最大 3000 字)
    conv_lines = []
    for r in conversation:
        role = r.get("role", "?")
        txt = (r.get("text") or r.get("content") or "")[:500]
        conv_lines.append(f"{role}: {txt}")
    conv_text = "\n".join(conv_lines)[:3000]

    prompt = EXTRACT_PROMPT.format(
        conversation=conv_text,
        wiki_snippets=(wiki_snippets or "(なし)")[:3000],
    )
    try:
        resp = await http.post(
            f"{litellm_url}/v1/chat/completions",
            headers={"Authorization": f"Bearer {litellm_key}"},
            json={
                "model": model,
                "messages": [{"role": "user", "content": prompt}],
                "max_tokens": 800,
                "temperature": 0.2,
            },
            timeout=60.0,
        )
        resp.raise_for_status()
        content = resp.json()["choices"][0]["message"]["content"]
        # JSON 抽出 (```json ... ``` 囲まれている場合も許容)
        import re as _re
        m = _re.search(r"\[[\s\S]*\]", content)
        if not m:
            return None  # ★2026-06-07 評価: JSON array 不在 = 抽出失敗。genuine empty の [] と区別
        arr = json.loads(m.group(0))
        return arr if isinstance(arr, list) else None
    except Exception as e:
        logger.warning(f"extract_insights failed: {e}")
        return None  # ★失敗 = None → run_scan が last_scan を進めず再スキャン (silent data loss 防止)


# ─── 保存 ─────────────────────────────────────────────
def save_insight(
    user_id: str,
    user_display: Optional[str],
    insight: dict,
    scanned_range: tuple[str, str],
) -> dict:
    _ensure_dir()
    rec = {
        "id": f"{datetime.now().strftime('%Y-%m-%d')}_{uuid.uuid4().hex[:6]}",
        "timestamp": datetime.now().astimezone().isoformat(timespec="seconds"),
        "user_id": user_id,
        "user_display": user_display,
        "category": insight.get("category", "other"),
        "insight": insight.get("insight", ""),
        "proposed_wiki_patch": insight.get("proposed_wiki_patch", ""),
        "source_snippet": insight.get("source_snippet", "")[:400],
        "scanned_range": list(scanned_range),
        "status": "pending",
    }
    path = _today_file()
    with locked(path):
        append_jsonl(path, rec)
    return rec


# ─── レビュー用 (/clone-learning) ─────────────────────────
def _iter_all_records():
    if not LEARNING_DIR.exists():
        return
    for f in sorted(LEARNING_DIR.glob("*.jsonl"), reverse=True):
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
    out = []
    for r in _iter_all_records():
        if r.get("status") == "pending":
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
    if new_status not in ("pending", "accepted", "rejected", "noted"):
        raise ValueError(new_status)
    for f in sorted(LEARNING_DIR.glob("*.jsonl")):
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
                        r["status"] = new_status
                        r["updated"] = datetime.now().astimezone().isoformat(timespec="seconds")
                        changed = True
                    new_lines.append(json.dumps(r, ensure_ascii=False))
                except Exception:
                    new_lines.append(line)
            if changed:
                write_text_atomic(f, "\n".join(new_lines) + "\n")
                return True
    return False


def get_entry_by_id(fid: str) -> Optional[dict]:
    """指定 id の learning entry を返す. 見つからなければ None.

    ★2026-05-25: escalate_to_system 用に追加 (= item content を抽出して system_issue 化)。
    """
    for f in sorted(LEARNING_DIR.glob("*.jsonl"), reverse=True):
        try:
            for line in f.read_text(encoding="utf-8").splitlines():
                line = line.strip()
                if not line:
                    continue
                try:
                    r = json.loads(line)
                except Exception:
                    continue
                if r.get("id") == fid:
                    return r
        except Exception:
            continue
    return None


def escalate_to_system(fid: str, note: str = "", reviewer: str = "umiyama") -> Optional[str]:
    """★2026-05-25 海山指示: learning item を system_issue に「格上げ」分類変更.

    用途: LLM が response_quality として抽出した item が実は bot crash / error
    fallback / retrieval bug など **システム不備** だった場合の reclassify。

    動作:
      1. learning item を読み (= fid)
      2. system_issues.add_entry を呼んで新 entry 作成
         description = insight + 抽出元 会話
         expected    = proposed_wiki_patch + note (= 補足説明)
      3. 元 learning item は status="rejected" + reference note 付与

    Returns: 新 system_issue id (= "sysi_xxx") or None (= 元 item 見つからず)
    """
    entry = get_entry_by_id(fid)
    if not entry:
        logger.warning(f"escalate_to_system: id={fid} not found in learning")
        return None

    insight = entry.get("insight", "").strip()
    snippet = entry.get("source_snippet", "").strip()
    patch = entry.get("proposed_wiki_patch", "").strip()
    note = (note or "").strip()

    # description = insight + 抽出元会話
    desc_parts = []
    if insight:
        desc_parts.append(insight)
    if snippet:
        desc_parts.append("\n[抽出元 会話]\n" + snippet)
    description = "\n".join(desc_parts) if desc_parts else f"(escalated from learning #{fid})"

    # expected = patch + 補足
    exp_parts = []
    if patch:
        exp_parts.append("[LLM 提案 wiki patch]\n" + patch)
    if note:
        exp_parts.append("[補足]\n" + note)
    expected = "\n\n".join(exp_parts)

    # system_issues に新規作成
    try:
        from services import system_issues
        sysi_id = system_issues.add_entry(description, expected=expected, reviewer=reviewer)
    except Exception as e:
        logger.exception(f"escalate_to_system: system_issues.add_entry failed: {e}")
        return None

    # 元 learning item を rejected に + reference note
    ref_note = f"→ system_issue {sysi_id} に再分類"
    update_status(fid, "rejected")
    try:
        add_comment(fid, ref_note, reviewer=reviewer)
    except Exception:
        pass

    logger.info(f"escalated learning {fid} → system_issue {sysi_id}")
    return sysi_id


def add_manual_entry(insight: str, proposed_wiki_patch: str = "", reviewer: str = "umiyama") -> str:
    """★2026-05-25 海山指示: ダッシュボード直接入力で 品質改善 entry を作成.

    LLM auto-discovery (= nightly scan) と並走し、海山が手動で
    「この回答こう直したい」「この wiki 追記したい」を直接登録する用。
    既存 review flow (= accept/reject/noted/comment + patch 編集) はそのまま使える。

    Args:
        insight: 改善内容の自由記述 (= 必須)
        proposed_wiki_patch: wiki に追記 / 修正したい内容 (= 任意)
        reviewer: 登録者 (default umiyama)

    Returns:
        生成された entry id (= "manual_xxx")
    """
    _ensure_dir()
    insight = (insight or "").strip()
    if not insight:
        raise ValueError("insight は必須")

    fname = LEARNING_DIR / f"{datetime.now().strftime('%Y-%m')}.jsonl"
    rec = {
        "id": f"manual_{uuid.uuid4().hex[:12]}",
        "timestamp": datetime.now().astimezone().isoformat(timespec="seconds"),
        "category": "manual_quality",
        "insight": insight,
        "source_snippet": "(海山がダッシュボードから直接入力)",
        "proposed_wiki_patch": (proposed_wiki_patch or "").strip(),
        "status": "pending",
        "user_id": reviewer,
        "manual_entry": True,
    }
    with locked(fname):
        append_jsonl(fname, rec)
    logger.info(f"manual learning entry: {rec['id']} by {reviewer}")
    return rec["id"]


def update_patch(fid: str, new_patch: str, reviewer: str = "umiyama") -> bool:
    """★2026-05-25 海山指示: proposed_wiki_patch を編集可能化.

    Dashboard で 提案 wiki patch を直接修正 → そのまま record に上書き保存。
    旧 patch は patch_history list に保管 (= 後で diff 比較可能)。
    """
    for f in sorted(LEARNING_DIR.glob("*.jsonl")):
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
                        old_patch = r.get("proposed_wiki_patch", "")
                        if old_patch != new_patch:
                            # 旧 patch を history に保存
                            history = r.get("patch_history", [])
                            history.append({
                                "ts": datetime.now().astimezone().isoformat(timespec="seconds"),
                                "reviewer": reviewer,
                                "old_patch": old_patch,
                            })
                            r["patch_history"] = history[-10:]  # 最新 10 件
                            r["proposed_wiki_patch"] = new_patch
                            r["updated"] = datetime.now().astimezone().isoformat(timespec="seconds")
                            changed = True
                    new_lines.append(json.dumps(r, ensure_ascii=False))
                except Exception:
                    new_lines.append(line)
            if changed:
                write_text_atomic(f, "\n".join(new_lines) + "\n")
                return True
    return False


# ─── ★2026-05-12: コメント機能 ───
def add_comment(fid: str, comment: str, reviewer: str = "umiyama") -> bool:
    """clone_learning レコードにコメントを追記"""
    from datetime import datetime as _dt
    for f in sorted(LEARNING_DIR.glob("*.jsonl")):
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
                        existing = r.get("comments") or []
                        if not isinstance(existing, list):
                            existing = []
                        existing.append({
                            "ts": _dt.now().astimezone().isoformat(timespec="seconds"),
                            "by": reviewer,
                            "text": comment,
                        })
                        r["comments"] = existing
                        changed = True
                    new_lines.append(json.dumps(r, ensure_ascii=False))
                except Exception:
                    new_lines.append(line)
            if changed:
                write_text_atomic(f, "\n".join(new_lines) + "\n")
                return True
    return False


CATEGORY_EMOJI = {
    "fact": "📊",
    "correction": "⚠️",
    "decision": "🧭",
    "style": "💬",
    "other": "💡",
    "response_quality": "🔧",  # ★2026-05-07: bot 応答品質の自己改善ループ
}


def summary(limit: int = 10) -> str:
    pending = list_pending(limit)
    if not pending:
        return "未レビューの会話発見はありません 👍"
    lines = [f"# 未レビュー会話発見 ({len(pending)}件)", ""]
    for r in pending:
        cat = r.get("category", "other")
        emoji = CATEGORY_EMOJI.get(cat, "💡")
        display = r.get("user_display") or r["user_id"][:8]
        ts = r["timestamp"][:16].replace("T", " ")
        lines.append(f"{emoji} `{r['id']}`  {ts}  {display} ({cat})")
        lines.append(f"  {r.get('insight','')[:120]}")
        if r.get("proposed_wiki_patch"):
            lines.append(f"  → {r['proposed_wiki_patch'][:100]}")
        lines.append("")
    lines.append("詳細: /clone-learning <id>")
    lines.append("取込: /clone-learning-accept <id>")
    lines.append("見送: /clone-learning-reject <id>")
    return "\n".join(lines)


def detail(fid: str) -> str:
    r = find_by_id(fid)
    if not r:
        return f"見つかりません: {fid}"
    display = r.get("user_display") or r["user_id"][:12]
    cat = r.get("category", "other")
    emoji = CATEGORY_EMOJI.get(cat, "💡")
    lines = [
        f"# Learning {r['id']}  {emoji}",
        f"📅 {r['timestamp']}",
        f"👤 {display}",
        f"🏷  category: {cat}  / status: {r.get('status')}",
        "",
        "## 発見",
        r.get("insight", ""),
    ]
    if r.get("proposed_wiki_patch"):
        lines += ["", "## Wiki 提案", r["proposed_wiki_patch"]]
    if r.get("source_snippet"):
        lines += ["", "## 会話スニペット", r["source_snippet"]]
    return "\n".join(lines)


# ─── ★応答品質の自動評価 (2026-05-07 追加、自己改善ループ) ─────────────
def _pick_judge() -> str:
    """本番 bot と別系列の judge alias (§1.15 self-eval loop 遮断)。
    共通実装 scripts/clone_improve_lib.pick_cross_family_judge に委譲、失敗時は安全側。"""
    try:
        import sys as _s
        from pathlib import Path as _P
        _r = str(_P(__file__).resolve().parent / "scripts")
        if _r not in _s.path:
            _s.path.insert(0, _r)
        from clone_improve_lib import pick_cross_family_judge
        return pick_cross_family_judge()
    except Exception:
        # bot 本番既定は smart-gpt (OpenAI) なので、判定不能時は Claude 側に倒す
        return "smart"


async def extract_response_quality_issues(
    http: httpx.AsyncClient,
    litellm_url: str,
    litellm_key: str,
    msgs: list[dict],
    model: str = "smart",
) -> list[dict]:
    """会話 pair (user → bot) を LLM が評価し、問題ありの応答を抽出。

    自己改善ループの主要 input。検出すべき問題:
    - misunderstanding: 質問の意図を取り違えた
    - too_passive: 「データ無い」と引きすぎ (推論補完で答えるべきだった)
    - wrong_data: 数字・固有名詞が違う
    - too_questioning: 質問返しが多すぎ (尋問になった)
    - tone_off: トーンが冷たい / 押し付け / 説教臭い

    返り値: [{user_msg_excerpt, bot_reply_excerpt, issue_category, issue, improvement}, ...]
    """
    # user → assistant の連続ペアを抽出 (直近 10 ペアまで)
    pairs: list[tuple[dict, dict]] = []
    last_user = None
    for m in msgs:
        role = m.get("role")
        if role == "user":
            last_user = m
        elif role == "assistant" and last_user:
            pairs.append((last_user, m))
            last_user = None
    pairs = pairs[-10:]  # 直近 10 ペア
    if not pairs:
        return []

    # LLM プロンプト用の整形
    pair_blocks = []
    for i, (u, a) in enumerate(pairs, start=1):
        ut = (u.get("text") or "")[:300]
        at = (a.get("text") or "")[:600]
        ts = (u.get("timestamp") or "")[:16]
        pair_blocks.append(f"### Pair {i} ({ts})\nUSER: {ut}\nBOT: {at}")
    pairs_text = "\n\n".join(pair_blocks)

    prompt = (
        "あなたは OWNDAYS 社長・海山丈司の AI 分身「うみやまAI」の応答品質審査官。\n"
        "下記の会話 pair (user→bot) について、bot 側の応答品質に問題がある pair だけ JSON で返してください。\n\n"
        "問題カテゴリ (どれか 1 つ):\n"
        "- misunderstanding: 質問の意図を取り違えた\n"
        "- too_passive: 「データ無い」「分からない」と素直に言いすぎ (推論補完で答えるべきだった)\n"
        "- wrong_data: 数字・固有名詞・事実関係が間違ってる疑い\n"
        "- too_questioning: 質問返しが多すぎ (尋問になった、最初に答えず聞き返した)\n"
        "- tone_off: 冷たい / 押し付けがましい / 説教臭い / 機械的\n"
        "- other: 上記以外で改善余地ある (具体的に書く)\n\n"
        "判定ガイド:\n"
        "- 問題なしのペアは無視 (出力しない)\n"
        "- 「明確に答えた + 海山らしい温度 + 数字正確」 = 問題なし\n"
        "- 「データ無い」と素直に断ったが、文脈から推論で答えられたケース = too_passive\n"
        "- 質問返し 1 つだけは OK、2 つ以上連発で答え無し = too_questioning\n\n"
        "出力 JSON 形式 (問題ありの pair のみ、問題なしなら []):\n"
        "[\n"
        "  {\n"
        "    \"pair_index\": 1,\n"
        "    \"user_msg_excerpt\": \"<最初 80 字>\",\n"
        "    \"bot_reply_excerpt\": \"<最初 80 字>\",\n"
        "    \"issue_category\": \"misunderstanding\",\n"
        "    \"issue\": \"<具体的に何が問題か、80字以内>\",\n"
        "    \"improvement\": \"<次回どう答えるべきか、海山スタイルで、120字以内>\"\n"
        "  }\n"
        "]\n\n"
        "# 会話ログ\n"
        f"{pairs_text}\n\n"
        "JSON のみ返答。説明文・前置きは絶対に付けない。"
    )

    try:
        resp = await http.post(
            f"{litellm_url}/v1/chat/completions",
            headers={"Authorization": f"Bearer {litellm_key}"},
            json={
                "model": model,
                "messages": [{"role": "user", "content": prompt}],
                "max_tokens": 2000,
                "temperature": 0.2,
            },
            timeout=180.0,
        )
        resp.raise_for_status()
        text = resp.json()["choices"][0]["message"]["content"]
    except Exception as e:
        logger.warning(f"response_quality LLM failed: {e}")
        return []

    # JSON 抜き出し
    s = text.strip()
    if "```json" in s:
        s = s.split("```json", 1)[1].split("```", 1)[0].strip()
    elif s.startswith("```"):
        s = s.split("```", 1)[1].split("```", 1)[0].strip()
    try:
        data = json.loads(s)
    except Exception:
        logger.warning(f"response_quality JSON parse failed: {text[:200]}")
        return []
    if not isinstance(data, list):
        return []
    out = []
    for item in data:
        if isinstance(item, dict) and item.get("issue_category"):
            out.append(item)
    return out


# ─── メイン: nightly スキャン ─────────────────────────
async def run_scan(
    http: httpx.AsyncClient,
    litellm_url: str,
    litellm_key: str,
    brain,  # BrainWiki instance (for wiki snippets)
    model: str = "fast-gpt",
    max_users: int = 50,
    quality_model: str = "",  # 空なら pick_cross_family_judge で本番 bot と別系列を自動選択
) -> int:
    """全ユーザの新メッセージを走査し insight を抽出・保存。返り値: 保存件数

    ★2026-05-07: 既存の insight 抽出 (発見/事実/訂正/style/other) に加えて、
    `extract_response_quality_issues` で**応答品質の問題**も自動評価。
    検出されたら category='response_quality' として clone_learning に保存
    → 翌日のダイジェストで海山がレビューしやすい形に。
    """
    new_msgs = _iter_new_messages_per_user()
    saved = 0
    scanned_users = []
    failed_users = 0  # ★2026-06-07 評価: 抽出失敗 user は last_scan を進めず別計上 (再スキャン)
    for i, (user_id, msgs) in enumerate(new_msgs.items()):
        if i >= max_users:
            break
        # user/assistant ペアで少なくとも 1 ラウンドあるか
        if len([m for m in msgs if m.get("role") == "user"]) < 1:
            continue
        # Wiki snippets: 最終 user message でベクトル検索
        last_user_msg = next(
            (m.get("text", "") for m in reversed(msgs) if m.get("role") == "user"),
            "",
        )
        wiki_snippets = ""
        if last_user_msg and brain is not None and getattr(brain, "index", None):
            try:
                hits = await brain.index.search(
                    last_user_msg[:300], n_results=3, collection="wiki"
                )
                wiki_snippets = "\n\n".join(
                    f"### {h.get('source','')}\n{(h.get('content') or '')[:500]}"
                    for h in hits
                )[:2500]
            except Exception as e:
                logger.warning(f"wiki snippet search failed: {e}")

        insights = await extract_insights(
            http, litellm_url, litellm_key, msgs, wiki_snippets, model=model
        )
        # ★2026-06-07 評価: 抽出失敗 (None) は last_scan を進めず次回再スキャン (silent data loss 防止)。
        #   [] (genuine empty = 本当に発見ゼロ) は従来通り scanned 扱いで前進。
        if insights is None:
            failed_users += 1
            continue

        # ★応答品質も並行評価
        # ★2026-08-03 self-eval 修正: 既定を "smart-gpt" 固定にしていたが、本番 bot が
        # CLONE_PUBLIC_PROD_MODEL=smart-gpt (GPT-5.4) に変わったことで **judge と bot が同一
        # provider** になっていた (2026-06-07 のコメントは bot=Opus 時代の前提のまま陳腐化)。
        # 同日直した clone_style_regression と同じく pick_cross_family_judge で自動選択する。
        try:
            _qm = quality_model or _pick_judge()
            quality_issues = await extract_response_quality_issues(
                http, litellm_url, litellm_key, msgs, model=_qm
            )
        except Exception as e:
            logger.warning(f"response_quality eval failed: {e}")
            quality_issues = []

        # quality_issue を insight 形式に変換して merge
        for q in quality_issues:
            insights.append({
                "category": "response_quality",
                "insight": (
                    f"[{q.get('issue_category')}] {q.get('issue', '')}\n"
                    f"USER: {q.get('user_msg_excerpt', '')}\n"
                    f"BOT: {q.get('bot_reply_excerpt', '')}"
                )[:500],
                "proposed_wiki_patch": q.get("improvement", "")[:400],
                "source_snippet": (
                    f"USER: {q.get('user_msg_excerpt', '')}\n"
                    f"BOT: {q.get('bot_reply_excerpt', '')}"
                )[:500],
            })

        if not insights:
            scanned_users.append(user_id)
            continue

        display = next(
            (m.get("user_display") for m in msgs if m.get("user_display")),
            None,
        )
        ts_start = msgs[0].get("timestamp", "")
        ts_end = msgs[-1].get("timestamp", "")
        for ins in insights:
            if not isinstance(ins, dict) or not ins.get("insight"):
                continue
            try:
                save_insight(user_id, display, ins, (ts_start, ts_end))
                saved += 1
            except Exception as e:
                logger.warning(f"save_insight failed: {e}")
        scanned_users.append(user_id)

    update_last_scan(scanned_users)
    logger.info(
        f"clone_learning scan done: {saved} insights saved, {len(scanned_users)} users scanned, "
        f"{failed_users} 抽出失敗 (last_scan 据置 = 次回再スキャン)"
    )
    return saved
