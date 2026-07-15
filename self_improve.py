"""
self_improve.py — 自己改善ループ

会話ログを分析し、Botの回答品質を評価。
問題パターンを検出して改善策を自動適用する。

実行: 毎日深夜 or main.py から定期呼び出し
"""

import os
import json
import logging
from datetime import datetime, date
from pathlib import Path

import httpx

logger = logging.getLogger(__name__)

LITELLM_URL = "http://litellm:4000"
LITELLM_KEY = ""
IMPROVEMENT_LOG = Path("/app/data/brain/self_improve_log.jsonl")
SYSTEM_PROMPT_OVERRIDES = Path("/app/data/brain/system_prompt_patches.json")


async def analyze_conversations(
    r,  # redis connection
    http: httpx.AsyncClient,
    litellm_url: str,
    litellm_key: str,
    max_users: int = 10,
) -> list[dict]:
    """
    Redisの会話履歴を分析し、問題パターンを検出する。

    検出パターン:
    - Bot が「できません」「わかりません」「アクセスできません」と返答
    - ユーザーが同じ質問を繰り返している（回答に不満）
    - Driveにデータがあるのに「情報がありません」と返答
    - 意図検出が間違っている（calendar を聞いているのに wiki だけ等）
    """
    issues = []

    # 全ユーザーの会話キーを取得
    keys = []
    async for key in r.scan_iter("chat:*"):
        keys.append(key)
        if len(keys) >= max_users:
            break

    for key in keys:
        raw = await r.lrange(key, -20, -1)
        if not raw:
            continue

        messages = [json.loads(m) for m in raw]

        # 会話ペアを抽出
        pairs = []
        for i in range(0, len(messages) - 1, 2):
            if messages[i]["role"] == "user" and i + 1 < len(messages):
                pairs.append({
                    "user": messages[i]["content"],
                    "assistant": messages[i + 1]["content"],
                })

        if not pairs:
            continue

        # LLMで会話品質を評価
        conv_text = "\n".join(
            f"User: {p['user']}\nBot: {p['assistant'][:200]}"
            for p in pairs[-10:]  # 直近10ペア
        )

        eval_prompt = f"""以下はCEOとAIアシスタントの会話ログです。
AIの回答品質を評価し、問題点を特定してください。

【評価基準】
1. 「できません」「わかりません」「アクセスできません」等の拒否回答がないか
2. 実際にはデータがあるのに「情報がありません」と返していないか
3. ユーザーが同じ質問を繰り返していないか（=回答に不満があった可能性）
4. 質問の意図に合ったデータソースを使っているか
5. 回答が具体的か、それとも一般論だけか

【会話ログ】
{conv_text}

【回答形式】JSON配列で返してください。問題がなければ空配列 []
各問題: {{"type": "refusal|missing_data|repeat_question|wrong_source|vague", "user_msg": "...", "bot_msg_snippet": "...", "suggestion": "..."}}
"""

        try:
            resp = await http.post(
                f"{litellm_url}/v1/chat/completions",
                headers={"Authorization": f"Bearer {litellm_key}"},
                json={
                    # ★2026-06-07 評価: legacy assistant (default=GPT-4o) 応答を fast=GPT-4o で
                    #   採点 = 同系列 self-eval loop。別 version GPT-5.4 (smart-gpt) で分離 (§4)。
                    "model": os.getenv("SELF_IMPROVE_EVAL_MODEL", "smart-gpt"),
                    "messages": [{"role": "user", "content": eval_prompt}],
                    "max_tokens": 1000,
                    "temperature": 0,
                },
                timeout=15.0,
            )
            resp.raise_for_status()
            text = resp.json()["choices"][0]["message"]["content"].strip()

            import re
            match = re.search(r'\[.*\]', text, re.DOTALL)
            if match:
                found_issues = json.loads(match.group())
                if found_issues:
                    for issue in found_issues:
                        issue["user_key"] = key
                        issue["detected_at"] = datetime.now().isoformat()
                    issues.extend(found_issues)
        except Exception as e:
            logger.warning(f"Conversation analysis error for {key}: {e}")

    return issues


async def generate_improvements(
    http: httpx.AsyncClient,
    litellm_url: str,
    litellm_key: str,
    issues: list[dict],
) -> list[dict]:
    """
    検出した問題から、システム改善策を生成する。

    改善策の種類:
    - system_prompt: システムプロンプトの追記
    (★2026-06-07 評価C5: 旧 intent_keyword / drive_search は読み手不在の dead bucket → 廃止)
    """
    if not issues:
        return []

    issues_text = json.dumps(issues, ensure_ascii=False, indent=2)

    improve_prompt = f"""以下はAIアシスタントの会話で検出された問題リストです。
これらの問題を解決するための具体的なシステム改善策を提案してください。

【問題リスト】
{issues_text}

【改善策の種類】
1. system_prompt_addition: システムプロンプトに追加すべきルール
（★2026-06-07 評価C5: 旧 intent_keywords / drive_search_pattern は読み手不在の dead bucket だったため廃止）

【回答形式】JSON配列
各改善: {{"type": "system_prompt_addition", "content": "...", "reason": "..."}}
"""

    # 自己改善は定期的に走るためコスト敏感。fast (GPT-4o) を使用。
    improvement_model = os.getenv("SELF_IMPROVE_MODEL", "fast")
    text = ""
    try:
        resp = await http.post(
            f"{litellm_url}/v1/chat/completions",
            headers={"Authorization": f"Bearer {litellm_key}"},
            json={
                "model": improvement_model,
                "messages": [{"role": "user", "content": improve_prompt}],
                "max_tokens": 1500,
                "temperature": 0,
            },
            timeout=30.0,
        )
        resp.raise_for_status()
        text = resp.json()["choices"][0]["message"]["content"].strip()

        import re
        match = re.search(r'\[.*\]', text, re.DOTALL)
        if not match:
            logger.warning(
                f"Improvement generation: no JSON array in LLM response. "
                f"text={text[:300]!r}"
            )
            return []
        try:
            return json.loads(match.group())
        except json.JSONDecodeError as je:
            logger.warning(
                f"Improvement generation: JSON parse failed ({je}). "
                f"matched={match.group()[:300]!r}"
            )
            return []
    except Exception as e:
        logger.warning(
            f"Improvement generation error ({type(e).__name__}: {e!r})",
            exc_info=True,
        )

    return []


def _norm_addition(s: str) -> str:
    """system_prompt_addition の実質同文判定用の正規化 (★2026-07-05 監査)。
    LLM が毎晩生成する文言は空白・句読点・語尾だけ揺れて exact-match dedup を素通りし、
    patches.json が同義文で無限に太る (実測 196 件 ≈ 13.8K 字が毎 turn プロンプトに注入)。"""
    import re
    s = re.sub(r"\s+", "", s)                # 空白・改行差を無視
    s = re.sub(r"[。、．，.,!！?？]+$", "", s)  # 末尾句読点差を無視
    return s.lower()


# 毎 turn 注入される additions の上限 (超過時は最古から捨てる = 最新の学びを優先)
MAX_PROMPT_ADDITIONS = int(os.getenv("SELF_IMPROVE_MAX_ADDITIONS", "60"))


def _addition_denied(content: str) -> bool:
    """★2026-07-05 監査: 「捏造への招待」型 addition を決定論 regex で reject。

    夜間 LLM は「政府の公式発表…を参照して回答」「業界平均…から推定値を提供」等、
    この bot が持たないソースの参照や推定値の生成を教える指示を生成することがある
    (実例が patches.json に混入済み)。base prompt の「データが本当にどこにもない場合のみ
    『見つかりませんでした』」と正面衝突し、数字を扱う assistant に捏造を教えるため通さない。

    ★DA cross-check 反映 (誤爆 5/5 の初版を棄却): deny は「確実な肯定形の捏造招待」のみ。
    - 外部ソース参照は **外部 marker (政府/公式発表/ウェブ等) との共起を必須** に絞る
      (「knowledge/…を参照して回答」「会話履歴を参照して回答」等の内部参照指示は正当 = 通す)
    - 否定・禁止形 (「検索せず」「参照できない場合は正直に」) は招待ではない = 通す
    """
    import re
    # 否定・禁止の文は「〜するな」という正当ルール — deny しない (捏造招待は肯定形)
    if re.search(r"(せず|しない|できない|ない場合|禁止|やめる|使わない)", content):
        return False
    # (A) 外部ソース marker + 参照/検索 動詞の共起
    external = re.search(
        r"(政府|公式発表|外部|ウェブ|web|インターネット|ニュース|報道"
        r"|経済データベース|信頼できる.{0,12}(データベース|情報源|ソース))",
        content, re.IGNORECASE)
    refer = re.search(r"(参照|検索|調べ|取得)", content)
    if external and refer:
        return True
    # (B) 推定値の生成を教える指示
    return bool(re.search(r"(推定値を(作成|算出|提供)|業界平均.{0,25}(推定|算出|提供))", content))


async def apply_improvements(improvements: list[dict]) -> list[str]:
    """
    改善策を適用する。
    - system_prompt_addition → patches.json に追記
      (★2026-07-05 コメント修正: 「次回起動時に適用」は誤り。main.py が毎 turn
       load_system_prompt_patches() で読み直すため、保存した瞬間から全応答に注入される)
    (★2026-06-07 評価C5: 旧 intent_keywords / drive_search_pattern は dead bucket → 廃止)
    """
    applied = []

    # 既存パッチを読み込み
    patches = {}
    if SYSTEM_PROMPT_OVERRIDES.exists():
        try:
            patches = json.loads(SYSTEM_PROMPT_OVERRIDES.read_text())
        except Exception:
            pass

    if "system_prompt_additions" not in patches:
        patches["system_prompt_additions"] = []

    # ★2026-07-05 監査: exact-match dedup → 正規化 dedup (空白/句読点ゆれの同義文を弾く)
    seen = {_norm_addition(c) for c in patches["system_prompt_additions"]}
    rejected: list[str] = []

    for imp in improvements:
        imp_type = imp.get("type", "")
        content = imp.get("content", "")

        if imp_type == "system_prompt_addition" and content:
            if _addition_denied(content):
                logger.warning(f"addition を deny-filter で reject (外部ソース参照 / 推定値指示): {content[:80]}")
                rejected.append(content)
                continue
            key = _norm_addition(content)
            if key not in seen:
                seen.add(key)
                patches["system_prompt_additions"].append(content)
                applied.append(f"[SystemPrompt] {content[:60]}...")
        # ★2026-06-07 エージェント評価 C5: 旧 intent_keywords / drive_search_pattern は
        #   load_system_prompt_patches が読まない dead bucket (reader 不在、cross-check Fact-checker
        #   で立証) → 生成・適用を廃止。うみやまAI clone にも legacy assistant にも効かない出力で
        #   LLM token と「改善した気」(偽陽性) を浪費していた。system_prompt_additions のみ有効。

    # ★DA 指摘 (§1.18): deny reject を log だけの無音 skip にしない — LINE で loud 化。
    # 絞った regex で reject は稀のはず = alert 疲れにはならない。push 失敗は握る (改善 loop を殺さない)
    if rejected:
        try:
            from scripts.clone_improve_lib import line_push
            line_push(f"⚠️ self_improve: 改善 patch {len(rejected)} 件を deny-filter で reject "
                      f"(外部ソース参照/推定値の捏造招待型)\n例: {rejected[0][:100]}")
        except Exception:
            pass

    # 上限超過は最古から drop (毎 turn 注入コストの有界化)。黙って消さず log に残す (§1.18 思想)
    adds = patches["system_prompt_additions"]
    if len(adds) > MAX_PROMPT_ADDITIONS:
        dropped = adds[: len(adds) - MAX_PROMPT_ADDITIONS]
        patches["system_prompt_additions"] = adds[len(adds) - MAX_PROMPT_ADDITIONS:]
        logger.warning(
            f"system_prompt_additions {len(adds)} 件 > 上限 {MAX_PROMPT_ADDITIONS} → "
            f"最古 {len(dropped)} 件を drop: " + " / ".join(d[:40] for d in dropped[:5])
        )
        applied.append(f"[SystemPrompt] 上限超過で最古 {len(dropped)} 件を drop")

    # パッチを保存
    if applied:
        SYSTEM_PROMPT_OVERRIDES.parent.mkdir(parents=True, exist_ok=True)
        SYSTEM_PROMPT_OVERRIDES.write_text(
            json.dumps(patches, ensure_ascii=False, indent=2)
        )

    return applied


async def run_self_improve(
    r,
    http: httpx.AsyncClient,
    litellm_url: str,
    litellm_key: str,
) -> str:
    """自己改善ループの1サイクルを実行"""
    logger.info("=== 自己改善ループ開始 ===")

    # 1. 会話分析
    issues = await analyze_conversations(r, http, litellm_url, litellm_key)
    logger.info(f"検出された問題: {len(issues)} 件")

    if not issues:
        logger.info("問題なし。改善不要。")
        return "問題なし"

    # 2. 改善策生成
    improvements = await generate_improvements(http, litellm_url, litellm_key, issues)
    logger.info(f"生成された改善策: {len(improvements)} 件")

    # 3. 適用
    applied = await apply_improvements(improvements)
    logger.info(f"適用された改善: {len(applied)} 件")

    # 4. ログ記録
    log_entry = {
        "timestamp": datetime.now().isoformat(),
        "issues_found": len(issues),
        "improvements_generated": len(improvements),
        "applied": applied,
        "issues": issues,
        "improvements": improvements,
    }
    IMPROVEMENT_LOG.parent.mkdir(parents=True, exist_ok=True)
    with open(IMPROVEMENT_LOG, "a") as f:
        f.write(json.dumps(log_entry, ensure_ascii=False) + "\n")

    summary = f"問題{len(issues)}件検出 → 改善{len(applied)}件適用"
    logger.info(f"=== 自己改善ループ完了: {summary} ===")
    return summary


def load_system_prompt_patches() -> str:
    """保存されたシステムプロンプトパッチを読み込む"""
    if not SYSTEM_PROMPT_OVERRIDES.exists():
        return ""
    try:
        patches = json.loads(SYSTEM_PROMPT_OVERRIDES.read_text())
        additions = patches.get("system_prompt_additions", [])
        if additions:
            return "\n【自己改善による追加ルール】\n" + "\n".join(f"- {a}" for a in additions)
    except Exception:
        pass
    return ""
