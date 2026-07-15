"""
improvement_trigger.py — リアルタイム品質検知 & 自動改善トリガー

会話ターンごとに不満足回答を検知し、即座に改善ループを起動する。
self_improve.py の 6時間バッチ版とは別に、ターン毎の即時パイプライン。

検知フロー:
  1. パターンマッチ（無料・即時）: 拒否フレーズ/繰返し質問
  2. 軽量LLM評価（fast モデル、必要時のみ）: 満足度・改善案
  3. 改善パッチ生成 + 適用 + ログ
"""

import os
import json
import logging
import asyncio
from datetime import datetime, date
from pathlib import Path

import httpx

from self_improve import generate_improvements, apply_improvements, IMPROVEMENT_LOG

logger = logging.getLogger(__name__)

# 自動改善ログの保存先
SYSTEM_IMPROVEMENTS_DIR = Path(
    os.getenv("SYSTEM_IMPROVEMENTS_DIR", "/app/data/brain/system_improvements")
)
AUTO_IMPROVE_LOG = SYSTEM_IMPROVEMENTS_DIR / "auto_detected.jsonl"

# クールダウン（同一ユーザーで連続発火を防ぐ）
AUTO_IMPROVE_COOLDOWN_SEC = int(os.getenv("AUTO_IMPROVE_COOLDOWN_SEC", "300"))  # 5分

# パターン検知
REFUSAL_PATTERNS = [
    "できません",
    "わかりません",
    "情報がありません",
    "データには含まれていません",
    "確認できません",
    "アクセスできません",
    "見つかりませんでした",
    "お答えできません",
    "提供されたデータには",
    "提供された情報には",
    "具体的な数値は",
    "情報が不足",
    "詳細な情報がない",
    "直接アクセスできない",
    "読み込むことはできません",
]

DISSATISFACTION_PATTERNS = [
    "違う",
    "違います",
    "違いますよ",
    "間違ってる",
    "間違ってます",
    "そうじゃない",
    "それじゃない",
    "ちがう",
    "おかしい",
    "答えになってない",
    "答えてない",
    "質問に答えて",
    "もう一度",
    "もっかい",
]


def _norm(text: str) -> str:
    return (text or "").lower().strip()


def detect_patterns(user_msg: str, ai_reply: str, prev_user_msg: str = "") -> list[str]:
    """低コストのパターン検知。検知理由のリストを返す（空=問題なし）"""
    reasons: list[str] = []
    reply_norm = _norm(ai_reply)
    user_norm = _norm(user_msg)

    # 拒否フレーズ
    for p in REFUSAL_PATTERNS:
        if p in reply_norm:
            reasons.append(f"refusal:{p}")
            break  # 1つでタグ

    # ユーザーからの不満表現
    for p in DISSATISFACTION_PATTERNS:
        if p in user_norm:
            reasons.append(f"dissatisfaction:{p}")
            break

    # 繰り返し質問（直前とほぼ同じ）
    if prev_user_msg and user_msg:
        prev_norm = _norm(prev_user_msg)
        if prev_norm and (user_norm == prev_norm or (
            len(user_norm) > 10 and user_norm[:20] == prev_norm[:20]
        )):
            reasons.append("repeat_question")

    return reasons


async def evaluate_with_llm(
    http: httpx.AsyncClient,
    litellm_url: str,
    litellm_key: str,
    user_msg: str,
    ai_reply: str,
) -> dict:
    """fast モデルで満足度を評価し、改善案を得る（パターン検知が hit した時のみ呼ぶ）"""
    prompt = (
        "あなたは AI アシスタントの回答品質を評価する評価者です。\n"
        "次の1ターンを評価してください。\n\n"
        f"【User】: {user_msg[:500]}\n"
        f"【AI】: {ai_reply[:1500]}\n\n"
        "以下のJSON形式で返してください（他の文章は不要）:\n"
        "{\n"
        '  "satisfactory": true|false,\n'
        '  "issue_type": "refusal|missing_data|wrong_source|vague|hallucination|other|null",\n'
        '  "root_cause": "原因の1文（例: Wiki に該当知識がない / RAG が Wiki を拾えていない / 用語マッピング不足）",\n'
        '  "suggestion_system_prompt": "system_prompt に追加すべきルール（必要ない場合はnull）",\n'
        '  "suggestion_wiki": "Wiki に追加すべき知識（具体的に。不要ならnull）",\n'
        '  "severity": "low|medium|high"\n'
        "}\n\n"
        "判定基準:\n"
        "- 「できません」「わかりません」「情報がない」等の拒否回答は unsatisfactory\n"
        "- 具体的な数字・固有名詞で答えられるはずの質問に一般論で答えた場合も unsatisfactory\n"
        "- Wiki にあるはずの情報を「見つからない」と返している場合は RAG の問題\n"
    )
    try:
        resp = await http.post(
            f"{litellm_url}/v1/chat/completions",
            headers={"Authorization": f"Bearer {litellm_key}"},
            json={
                "model": "fast",
                "messages": [{"role": "user", "content": prompt}],
                "max_tokens": 500,
                "temperature": 0,
            },
            timeout=20.0,
        )
        resp.raise_for_status()
        text = resp.json()["choices"][0]["message"]["content"].strip()
        import re
        m = re.search(r"\{.*\}", text, re.DOTALL)
        if m:
            return json.loads(m.group())
    except Exception as e:
        logger.warning(f"LLM eval error: {e}")
    return {}


async def _check_cooldown(r, user_id: str) -> bool:
    """True なら cooldown 中（改善をスキップ）"""
    key = f"auto_improve_cooldown:{user_id}"
    exists = await r.get(key)
    if exists:
        return True
    await r.setex(key, AUTO_IMPROVE_COOLDOWN_SEC, "1")
    return False


def _append_human_log(
    source: str,
    user_id: str,
    user_msg: str,
    ai_reply: str,
    eval_result: dict,
    reasons: list[str],
    applied_patches: list[str],
) -> Path:
    """人間が読める system_improvements/YYYY-MM-DD.md に追記"""
    SYSTEM_IMPROVEMENTS_DIR.mkdir(parents=True, exist_ok=True)
    path = SYSTEM_IMPROVEMENTS_DIR / f"{date.today()}.md"
    ts = datetime.now().strftime("%H:%M:%S")

    patches_text = "\n".join(f"  - {p}" for p in applied_patches) if applied_patches else "  - (なし)"
    suggestion_sp = eval_result.get("suggestion_system_prompt") or "-"
    suggestion_wiki = eval_result.get("suggestion_wiki") or "-"

    entry = (
        f"\n## {ts} [auto] {source} (user={user_id[:8]})\n"
        f"- **検知理由**: {', '.join(reasons) or '-'}\n"
        f"- **issue_type**: {eval_result.get('issue_type', '-')}\n"
        f"- **severity**: {eval_result.get('severity', '-')}\n"
        f"- **root_cause**: {eval_result.get('root_cause', '-')}\n"
        f"- **user_msg**: {user_msg.strip()[:300]}\n"
        f"- **ai_reply**: {ai_reply.strip()[:300]}\n"
        f"- **suggestion (system_prompt)**: {suggestion_sp}\n"
        f"- **suggestion (wiki)**: {suggestion_wiki}\n"
        f"- **applied_patches**:\n{patches_text}\n"
        f"\n---\n"
    )
    with open(path, "a", encoding="utf-8") as f:
        f.write(entry)
    return path


def _append_machine_log(entry: dict) -> None:
    AUTO_IMPROVE_LOG.parent.mkdir(parents=True, exist_ok=True)
    with open(AUTO_IMPROVE_LOG, "a", encoding="utf-8") as f:
        f.write(json.dumps(entry, ensure_ascii=False) + "\n")


async def detect_and_improve(
    app,
    source: str,
    user_id: str,
    user_msg: str,
    ai_reply: str,
    prev_user_msg: str = "",
    force: bool = False,
) -> dict:
    """ターン毎の不満足検知 & 即時改善トリガー

    Args:
        source: "line" | "claude_ingest" | "manual_fix" 等
        force: True のときはパターン検知を飛ばしてLLM評価と改善生成を必ず実行
               （ユーザーが明示的に「システム修正」ボタンを押した場合など）

    Returns:
        {"triggered": bool, "reasons": [...], "applied_patches": [...]}
    """
    http = app.state.http
    litellm_url = os.getenv("LITELLM_URL", "http://litellm:4000")
    litellm_key = os.getenv("LITELLM_MASTER_KEY", "")

    # 1. パターン検知
    reasons = detect_patterns(user_msg, ai_reply, prev_user_msg=prev_user_msg) if not force else ["manual_fix"]
    if not reasons:
        return {"triggered": False, "reasons": [], "applied_patches": []}

    # 2. クールダウン
    if not force and await _check_cooldown(app.state.redis, user_id):
        logger.info(f"auto_improve: cooldown active for {user_id[:8]}, skip")
        return {"triggered": False, "reasons": reasons, "applied_patches": [], "skipped": "cooldown"}

    logger.info(
        f"auto_improve: detected {len(reasons)} pattern(s) for {user_id[:8]} src={source}: {reasons}"
    )

    # 3. LLM 評価で具体的な改善案を得る
    eval_result = await evaluate_with_llm(http, litellm_url, litellm_key, user_msg, ai_reply)

    if not force and eval_result.get("satisfactory") is True:
        # LLM が「これは問題ない」と判定したら（パターン誤検知）→ ログだけ残してスキップ
        logger.info(f"auto_improve: LLM deemed satisfactory despite patterns, skip")
        _append_machine_log({
            "timestamp": datetime.now().isoformat(),
            "source": source,
            "user_id": user_id[:8],
            "user_msg": user_msg,
            "ai_reply": ai_reply,
            "reasons": reasons,
            "eval": eval_result,
            "applied": [],
            "skipped": "llm_satisfied",
        })
        return {"triggered": False, "reasons": reasons, "applied_patches": [], "skipped": "llm_satisfied"}

    # 4. 改善策を生成（既存の generate_improvements を流用）
    issue = {
        "type": eval_result.get("issue_type", "unsatisfactory"),
        "user_msg": user_msg[:300],
        "bot_msg_snippet": ai_reply[:300],
        "suggestion": eval_result.get("root_cause") or "auto-detected unsatisfactory reply",
        "source": source,
        "user_key": f"chat:{user_id}",
        "detected_at": datetime.now().isoformat(),
    }

    improvements = await generate_improvements(http, litellm_url, litellm_key, [issue])
    applied = await apply_improvements(improvements) if improvements else []

    # 5. Wiki への追加提案があれば raw/notes に蓄積（後で compile される）
    wiki_suggestion = eval_result.get("suggestion_wiki")
    if wiki_suggestion and wiki_suggestion not in ("-", "null", "None", ""):
        try:
            brain = app.state.brain
            note_title = f"auto_wiki_suggestion_{datetime.now().strftime('%Y%m%d_%H%M%S')}"
            wiki_note = (
                f"【自動検知による Wiki 知識の追加提案】\n\n"
                f"ユーザー質問: {user_msg}\n\n"
                f"満足な回答が得られなかった理由: {eval_result.get('root_cause', '不明')}\n\n"
                f"追加すべき知識:\n{wiki_suggestion}\n"
            )
            # 明示的提案は smart モデルで即コンパイル
            await brain.ingest_note(user_id, wiki_note, title=note_title, model="smart")
            applied.append(f"[Wiki] {wiki_suggestion[:60]}...")
        except Exception as e:
            logger.warning(f"auto_improve: wiki ingest failed: {e}")

    # 6. ログ（機械用 & 人間用）
    _append_machine_log({
        "timestamp": datetime.now().isoformat(),
        "source": source,
        "user_id": user_id[:8],
        "user_msg": user_msg,
        "ai_reply": ai_reply,
        "reasons": reasons,
        "eval": eval_result,
        "improvements": improvements,
        "applied": applied,
    })
    _append_human_log(source, user_id, user_msg, ai_reply, eval_result, reasons, applied)

    # 7. self_improve の機械ログにも追記（/api/self-improve と統合）
    try:
        log_entry = {
            "timestamp": datetime.now().isoformat(),
            "mode": "realtime_auto",
            "source": source,
            "issues_found": 1,
            "improvements_generated": len(improvements),
            "applied": applied,
            "issues": [issue],
            "improvements": improvements,
        }
        IMPROVEMENT_LOG.parent.mkdir(parents=True, exist_ok=True)
        with open(IMPROVEMENT_LOG, "a", encoding="utf-8") as f:
            f.write(json.dumps(log_entry, ensure_ascii=False) + "\n")
    except Exception as e:
        logger.warning(f"auto_improve: improvement_log append failed: {e}")

    logger.info(
        f"auto_improve: triggered src={source} reasons={reasons} → "
        f"{len(improvements)} improvements, {len(applied)} applied"
    )
    return {
        "triggered": True,
        "reasons": reasons,
        "eval": eval_result,
        "improvements": improvements,
        "applied_patches": applied,
    }
