"""services/agent_core.py — 海山個人アシスタント (run_agent) の agentic 化コア
(★2026-07-20 個人エージェント評価 #1: 単発 RAG → bounded tool-loop + persona 常時注入)

main.py run_agent から呼ばれる (§1.12b: main.py は wiring のみ、ロジックはここ)。

設計 (レイテンシ回帰と本番事故を両方防ぐ):
- 既存の並列 prefetch (wiki/calendar/mail/drive) は round-0 context として**維持** —
  単純質問は従来どおり 1 回の completion で返る (ツールは「足りない時だけ」)
- tool-loop は bounded: 最大 MAX_ROUNDS round・1 round 最大 4 tool call・
  1 tool 40s timeout・全体 wall-clock budget (LINE reply token 対策)
- tools 付き呼び出しが**初回で失敗したら tools 無し (= 従来挙動) に自動 fallback** =
  本番の安全床は「現行と同じ」
- 内部 write tools (reminder/task/memory) は owner_memory の file 書込のみ =
  外部送信ゼロ (メール送信/カレンダー書込はしない。Google scope も readonly のまま)

adapter 注記 (§1.19): LiteLLM の OpenAI-compatible tool-calls format に依存する箇所は
run_tool_loop 内に閉じている (adapter、交換可能)。
"""
from __future__ import annotations

import asyncio
import inspect
import json
import logging
import os
import re
import time
from pathlib import Path

logger = logging.getLogger("agent_core")

MAX_ROUNDS = 3          # tool 実行 round の上限 (最終 round は tool_choice=none で強制回答)
MAX_CALLS_PER_ROUND = 4
TOOL_TIMEOUT_S = 40.0   # Drive 検索 (~34s) を収容 (残余 budget があればそこまで縮む)
# ★cross-check 3体一致: budget は round 境界だけでなく各 tool 実行前にも判定 (下の loop 参照)。
# LINE reply token (~1分ガイダンス) を意識し 55s。単発 LLM call 自体 (httpx 60s) は従来と同じ露出。
TOTAL_BUDGET_S = 55.0
TOOL_RESULT_MAX_CHARS = 12000  # prefetch 経路 (12K字/file) と同等の情報量を確保 (DA 指摘)

# 応答が空になった時の保険 (LINE の空 text 400 を防ぐ)
_EMPTY_REPLY_FALLBACK = "（応答を生成できませんでした。もう一度お試しください）"

# ─── tool 定義 (OpenAI function-calling format) ───

TOOLS = [
    {"type": "function", "function": {
        "name": "search_brain",
        "description": "Brain Wiki (知識ベース) を別クエリで追加検索する。既に提供済みの context で足りない時のみ使う。",
        "parameters": {"type": "object", "properties": {
            "query": {"type": "string", "description": "検索クエリ (日本語可)"}},
            "required": ["query"]},
    }},
    {"type": "function", "function": {
        "name": "search_drive",
        "description": "Google Drive の資料を検索して内容を読む。時間がかかる (30秒前後) ので、Drive の資料が本当に必要な時のみ使う。",
        "parameters": {"type": "object", "properties": {
            "query": {"type": "string", "description": "検索キーワード"}},
            "required": ["query"]},
    }},
    {"type": "function", "function": {
        "name": "get_calendar",
        "description": "Google カレンダーの予定を取得する (未提供の先の日程が必要な時)。",
        "parameters": {"type": "object", "properties": {
            "days": {"type": "integer", "description": "今日から何日分 (1-30)"}},
            "required": ["days"]},
    }},
    {"type": "function", "function": {
        "name": "get_mail",
        "description": "Gmail の直近メールを取得する (メール文脈が必要なのに未提供の時)。",
        "parameters": {"type": "object", "properties": {
            "days": {"type": "integer", "description": "直近何日分 (1-7)"}},
            "required": ["days"]},
    }},
    {"type": "function", "function": {
        "name": "create_reminder",
        "description": "指定日の朝 9:00 に LINE で届くリマインダーを設定する。海山が「リマインドして」「覚えておいて後で知らせて」と言った時に使う。当日それ以降の時刻指定はできない (翌日以降の朝9時のみ)。",
        "parameters": {"type": "object", "properties": {
            "date": {"type": "string", "description": "YYYY-MM-DD (今日以降)"},
            "title": {"type": "string", "description": "リマインダーの見出し (1行)"},
            "body": {"type": "string", "description": "本文 (任意、詳細や背景)"}},
            "required": ["date", "title"]},
    }},
    {"type": "function", "function": {
        "name": "add_task",
        "description": "海山のタスクリストに項目を追加する (「タスクに入れて」「TODO化して」等)。",
        "parameters": {"type": "object", "properties": {
            "text": {"type": "string", "description": "タスク内容 (簡潔に)"}},
            "required": ["text"]},
    }},
    {"type": "function", "function": {
        "name": "complete_task",
        "description": "タスクを完了にする (「◯◯終わった」「完了にして」等)。部分一致で最初の未完了タスクを閉じる。",
        "parameters": {"type": "object", "properties": {
            "match": {"type": "string", "description": "完了するタスクの一部の文言"}},
            "required": ["match"]},
    }},
    {"type": "function", "function": {
        "name": "remember",
        "description": "海山が明示的に「覚えておいて」と言った恒久情報を Owner Memory に保存する。",
        "parameters": {"type": "object", "properties": {
            "section": {"type": "string", "enum": ["facts", "preferences", "ongoing"],
                        "description": "facts=恒久事実 / preferences=嗜好 / ongoing=進行中の案件"},
            "text": {"type": "string", "description": "保存する内容 (60字以内)"}},
            "required": ["section", "text"]},
    }},
]


def _internal_executors() -> dict:
    """owner_memory の file 書込 tools (外部送信ゼロ)。"""
    from services import owner_memory as om

    def _remember(a: dict) -> str:
        section = a.get("section", "facts")
        if section not in om.SECTIONS:  # 失敗理由を dedup と区別 (Reviewer 指摘)
            return f"tool error: section は facts/preferences/ongoing のいずれか (受領: {section})"
        if not (a.get("text") or "").strip():
            return "tool error: text が空です"
        ok = om.add_entry(section, a.get("text", ""))
        return f"🧠 記憶しました: {a.get('text','')}" if ok else "既に同内容の記憶があります"

    def _add_task(a: dict) -> str:
        ok = om.add_task(a.get("text", ""))
        return f"✅ タスク追加: {a.get('text','')}" if ok else "同内容の未完了タスクが既にあります"

    def _complete(a: dict) -> str:
        done = om.complete_task(a.get("match", ""))
        return f"☑️ 完了: {done}" if done else f"未完了タスクに「{a.get('match','')}」が見つかりません"

    return {
        "create_reminder": lambda a: om.create_reminder(a.get("date", ""), a.get("title", ""), a.get("body", "")),
        "add_task": _add_task,
        "complete_task": _complete,
        "remember": _remember,
    }


def merge_executors(external: dict) -> dict:
    """main.py の fetcher executors + 内部 write tools を合成。"""
    ex = _internal_executors()
    ex.update(external or {})
    return ex


# ─── persona digest (identity/style/thinking の常時注入、mtime cache) ───

_persona_cache: dict = {"key": None, "value": ""}

_FRONTMATTER_RE = re.compile(r"\A---\n.*?\n---\n", re.DOTALL)


def _wiki_dir() -> Path:
    return Path(os.getenv("BRAIN_APP_ROOT", "/app")) / "data" / "brain" / "wiki"


def _excerpt(path: Path, max_chars: int) -> str:
    try:
        text = path.read_text(encoding="utf-8")
    except Exception:
        return ""
    text = _FRONTMATTER_RE.sub("", text).strip()
    if len(text) <= max_chars:
        return text
    cut = text[:max_chars]
    # 行境界で切る (文中でぶつ切りにしない)
    nl = cut.rfind("\n")
    if nl > max_chars // 2:
        cut = cut[:nl]
    return cut + "\n…(要約続きあり)"


def load_persona_digest(max_total: int = 2400) -> str:
    """identity/style/thinking の bounded digest。ファイル不在は '' (graceful)。"""
    d = _wiki_dir()
    files = [("identity.md", 1000), ("thinking.md", 800), ("style.md", 600)]
    try:
        key = tuple((f, (d / f).stat().st_mtime if (d / f).exists() else 0) for f, _ in files)
    except Exception:
        key = None
    if key is not None and _persona_cache["key"] == key:
        return _persona_cache["value"]
    parts = []
    for fname, cap in files:
        ex = _excerpt(d / fname, cap)
        if ex:
            label = {"identity.md": "価値観・信念", "thinking.md": "思考様式", "style.md": "文体・話し方"}[fname]
            parts.append(f"◆{label}\n{ex}")
    digest = "\n\n".join(parts)[:max_total]
    if key is not None:
        _persona_cache["key"] = key
        _persona_cache["value"] = digest
    return digest


# ─── system prompt 構築 ───

# 独立 part にして fallback (tools 無し) 時に verbatim 除去できるようにする
# (★cross-check DA: tools 無し mode に「必ず実行してから報告」が残ると捏造報告を誘発)
TOOL_GUIDANCE = (
    "【ツール使用指針】\n"
    "- 提供済み context で答えられるなら、ツールを呼ばずそのまま回答する (最速)。\n"
    "- context に無い情報が必要な時だけ検索ツールを使う。search_drive は遅いので本当に必要な時のみ。\n"
    "- 売上/客数/予算比などの業務データの数字は、この経路には来ない (別経路で確定値を返す)。"
    "万一この場に数字が無いのに聞かれたら、推測で作らず「確認する」と答える。\n"
    "- リマインダー/タスク/記憶の依頼には対応する書込ツールを**必ず実行してから**結果を報告する (実行せずに「設定しました」と言うのは禁止)。"
)


def build_system_prompt(live_context: str, prompt_patches: str, now_str: str) -> str:
    """run_agent の system prompt。persona digest + owner memory を常時注入。

    ★§1.15(a) 対象: 既存の【重要ルール】(AI 制限言及の禁止 = 実事故由来) は維持しつつ、
    捏造抑止 1 行 (2026-07-13 売上捏造事故の方向性) と tool 使用指針を追加。
    """
    from services import owner_memory as om
    persona = load_persona_digest()
    memory = om.load_memory_block()

    parts = [
        "あなたは「Umiyama AI Agent」— OWNDAYS CEO 海山丈司の専属 AI エージェントです"
        " (★2026-07-20 海山指示で正式名称化。社員向けの分身「うみやまAI」とは別物 = "
        "こちらは海山本人に仕える秘書)。\n"
        "24時間稼働し、スケジュール管理、メール確認、資料検索、知識ベース参照、"
        "リマインダー/タスク管理が可能です。機能一覧を聞かれたら /help を案内してください。"
    ]
    if persona:
        parts.append(
            "【主人の理解 (人格ダイジェスト)】\n"
            "以下は海山本人の人格・思考・文体の要約。彼を深く理解した秘書として、"
            "彼の判断軸や好みに沿った提案・要約に活かすこと (本人へのなりきりではない):\n"
            + persona
        )
    if memory:
        parts.append(
            "【恒久メモリー (過去の会話から蓄積)】\n" + memory
        )
    parts.append(
        "【重要ルール】\n"
        "- 以下に取得済みデータが含まれています。このデータを使って具体的に回答してください。\n"
        "- 「AIなのでファイルを読めません」「直接アクセスできません」等の回答は絶対に禁止です。\n"
        "- 「ファイルの直接読み込みや学習は行えません」等のAI制限に言及する回答も禁止です。\n"
        "- 「Brain Wikiに該当情報がありません」ではなく、Driveのデータが提供されていればそれを使え。\n"
        "- スプレッドシートやCSVデータが提供されている場合は、具体的な数値（金額、%、件数等）を引用して回答せよ。\n"
        "- ファイルを要求された場合は、Google DriveのURLリンクを提供せよ。\n"
        "- 取得データに無い数値・固有名詞を推測で作らない。探しても無いものは「データに見当たらない」と明示する。\n"
        "- データが本当にどこにもない場合のみ「該当する情報が見つかりませんでした」と答える。\n"
        "- システム/コードの修正・開発指示は自分では実行できない (この制約は上の AI 制限言及禁止より優先) — "
        "「/claude <指示> で開発タスクに回せます」と案内し、対応した風の応答は絶対にしない。\n"
        "- 質問でない長文メモ・URL だけの投げ込みには、長い論評をせず 2-3 行の要点確認 + "
        "記録済みの旨だけ返す (会話は自動で Wiki に蓄積される。専用ページ化は /teach)。\n"
        "- 日本語で簡潔に応答してください。\n"
        f"- 現在時刻: {now_str}"
    )
    parts.append(TOOL_GUIDANCE)
    return "\n\n".join(parts) + f"{prompt_patches}{live_context}"


# ─── tool loop (adapter: LiteLLM OpenAI-compatible tool-calls) ───

async def _exec_tool(executors: dict, name: str, args: dict, timeout: float = TOOL_TIMEOUT_S) -> str:
    fn = executors.get(name)
    if fn is None:
        return f"tool error: unknown tool {name}"
    try:
        res = fn(args)
        if inspect.isawaitable(res):
            res = await asyncio.wait_for(res, timeout=timeout)
        return str(res)[:TOOL_RESULT_MAX_CHARS]
    except Exception as e:
        logger.warning(f"tool {name} failed: {type(e).__name__}: {e}")
        return f"tool error: {type(e).__name__}: {str(e)[:200]}"


def _strip_tool_guidance(msgs: list[dict]) -> list[dict]:
    """tools 無し fallback 用: system message から【ツール使用指針】を除去
    (「必ず実行してから報告」が tools 無し mode に残ると捏造報告を誘発 = DA 指摘)。"""
    out = [dict(m) for m in msgs]
    if out and out[0].get("role") == "system":
        out[0]["content"] = (out[0].get("content") or "").replace(TOOL_GUIDANCE, "")
    return out


async def run_tool_loop(
    http,
    litellm_url: str,
    litellm_key: str,
    model: str,
    messages: list[dict],
    executors: dict,
    max_tokens: int = 4000,
) -> str:
    """bounded tool-loop。

    ★cross-check 反映 (2026-07-20):
    - final round は tools を**残して** tool_choice='none' — Anthropic Messages API は
      tool_use/tool_result 履歴を含む request に tools 定義を要求する (外すと 400)。
    - budget は round 境界 + 各 tool 実行前の両方で判定、tool timeout は残余から導出。
    - round0 失敗 → tools 無し + 指針除去 = 現行単発挙動へ fallback (安全床)。
    - round≥1 失敗 → tool 結果を保持したまま tool_choice='none' で 1 回 degrade 再試行
      (書込 tool 実行済みなのにエラー文だけ返る不整合を回避)。
    """
    msgs = list(messages)
    start = time.monotonic()
    headers = {"Authorization": f"Bearer {litellm_key}"}
    url = f"{litellm_url}/v1/chat/completions"

    def _remaining() -> float:
        return TOTAL_BUDGET_S - (time.monotonic() - start)

    async def _call(payload: dict) -> dict:
        resp = await http.post(url, headers=headers, json=payload)
        resp.raise_for_status()
        return resp.json()["choices"][0]["message"]

    msg: dict = {}
    for round_no in range(MAX_ROUNDS + 1):
        final_round = round_no >= MAX_ROUNDS or _remaining() <= 0
        payload = {"model": model, "messages": msgs, "max_tokens": max_tokens, "tools": TOOLS}
        if final_round:
            payload["tool_choice"] = "none"
        try:
            msg = await _call(payload)
        except Exception as e:
            if round_no == 0:
                # tools 起因の失敗切り分け: tools 無し = 現行挙動へ fallback (安全床)
                logger.warning(f"tool-loop round0 failed, fallback to plain: {type(e).__name__}: {e}")
                msg = await _call({"model": model, "messages": _strip_tool_guidance(msgs),
                                   "max_tokens": max_tokens})
            else:
                # tool 実行済みの途中失敗: 結果を無駄にせず強制回答へ degrade
                logger.warning(f"tool-loop round{round_no} failed, degrade to forced answer: {type(e).__name__}: {e}")
                msg = await _call({"model": model, "messages": msgs, "max_tokens": max_tokens,
                                   "tools": TOOLS, "tool_choice": "none"})
        tool_calls = msg.get("tool_calls") or []
        if not tool_calls or final_round:
            return msg.get("content") or _EMPTY_REPLY_FALLBACK
        # assistant (tool_calls) を履歴に積み、各 tool を実行
        msgs.append({"role": "assistant", "content": msg.get("content") or "",
                     "tool_calls": tool_calls[:MAX_CALLS_PER_ROUND]})
        for tc in tool_calls[:MAX_CALLS_PER_ROUND]:
            fname = tc.get("function", {}).get("name", "")
            try:
                fargs = json.loads(tc.get("function", {}).get("arguments") or "{}")
            except Exception:
                fargs = {}
            logger.info(f"agent tool call: {fname}({json.dumps(fargs, ensure_ascii=False)[:200]})")
            rem = _remaining()
            if rem <= 0:  # ★round 内でも budget 判定 (直列 4 tool × 40s の暴走防止)
                result = "tool error: time budget exceeded (残りのツールは実行せず回答へ)"
            else:
                result = await _exec_tool(executors, fname, fargs, timeout=min(TOOL_TIMEOUT_S, rem))
            msgs.append({"role": "tool", "tool_call_id": tc.get("id", ""), "content": result})
    return msg.get("content") or _EMPTY_REPLY_FALLBACK  # 保険 (通常は final_round で return 済)
