"""services/voice_tools.py — Vapi 通話中の PB retrieval tool (★2026-07-12 音声フェーズ Phase 1).

海山「PB 連携 AI が音声で会話できる」フェーズの本丸 = 通話中に brain_search tool で
PB の知識 (wiki chroma 索引) を引き、「本人声 × PB 知識 × 自然会話」を成立させる。

Vapi custom tool (server tool) 仕様 (2026-07 公式 docs + live OpenAPI spec 裏取り済):
  - assistant config: model.tools[] に CreateFunctionToolDTO {type:function,
    function:{name,description,parameters}} を inline 定義 (transient で正式サポート)
  - tool 実行時: server.url へ message.type="tool-calls"。payload はガイドの flat 形
    [{id,name,arguments(dict)}] と現行 spec の OpenAI 入れ子形
    [{id,type,function:{name,arguments(str)}}] が公式内で混在 → 両対応は必須
  - 応答: {"results": [{"toolCallId","name","result"}]} (spec required = toolCallId+name)

セキュリティ設計 (§1.15 cross-check 3 体反映、2026-07-12):
  - **tool 定義に server ブロックを持たせない** (Fact-check): tool.server.secret は
    2025-08-30 に Vapi spec から削除された legacy で届く保証が無い。server を省略すると
    tool-calls は公式 fallback 連鎖 (tool.server → assistant.server → phoneNumber.server)
    で assistant.server.url へ届く = 本番実績層 + 経路別 secret (phone=VAPI_SECRET /
    web=VAPI_WEB_SECRET) がそのまま効く。secret を tool に埋めない = web-config (ブラウザに
    config をそのまま返す) へ電話用 secret が漏れる事故 (Reviewer/DA F1 BLOCKER) も
    構造的に起きない。
  - tool は **trusted config にのみ** 付与 (untrusted 発信者の縮退 config には付けない)。
    webhook 側 tool-calls も X-Vapi-Secret 必須の二重ゲート (main.py)。
  - 検索は chroma wiki collection = personal/ は索引時点で除外済だが **interview/ と
    clone_visibility:private を含む** (§1.17 の 6 系統目の意図的例外 = 海山専用 trusted
    経路のみが呼ぶ)。漏洩疑い時は VAPI_SECRET / VAPI_WEB_SECRET を rotate。
  - chroma へは同一プロセス内の read query (§1.5 の並行書込禁止とは別物、既存 retrieval と同経路)。

adapter (交換可能): Vapi 固有 format はこの module に隔離 (§1.19②)。検索本体は brain.index
(BrainIndex.search) を再利用し、音声プロバイダを替えてもこの整形層だけ差し替えれば良い。
整形は決定論 (LLM 不使用)。
"""
from __future__ import annotations

import json
import logging
import re

logger = logging.getLogger(__name__)

TOOL_NAME = "brain_search"

# system prompt への使い方ガイド (trusted config のみに追記)。
# ★DA 反映: ①雑談が主目的 (Q&A モード化で蒸留材料が痩せるのを防ぐ) ②数字・固有名詞は
# 丸めない (「咀嚼して返す」だけだと 12.4億→約12億 等の音声数字事故を促してしまう)
PROMPT_GUIDANCE = (
    "\n\n【brain_search ツール】"
    "雑談が主目的。相手の話を聞き出すのが第一で、調べ物 Q&A モードには切り替えない。"
    "相手が事実・数字・過去の経緯・社内の状況 (売上/店舗/戦略/人/決定事項) に触れた時だけ、"
    "答える前に brain_search で Brain を引いて裏を取る。当てずっぽうで数字や固有名詞を言わない。"
    "検索結果は会話の材料 — 文章はそのまま読み上げず自分の言葉で短く咀嚼する。"
    "ただし数字・固有名詞・日付は丸めず省略せず、検索結果の値をそのまま正確に言う。"
    "見つからなければ「手元に無い、あとで調べて送る」と正直に。"
)


def tool_definition() -> dict:
    """Vapi model.tools[] entry (CreateFunctionToolDTO)。

    ★server ブロックは意図的に無し (module docstring のセキュリティ設計参照)。
    messages = tool 実行中の filler (DA: 検索 1-3 秒の無音は電話では長い)。"""
    return {
        "type": "function",
        "function": {
            "name": TOOL_NAME,
            "description": (
                "海山の Personal Brain (社内 wiki: 売上・店舗・商圏・戦略・判断軸・人物・"
                "過去の決定・会議録) を検索する。事実・数字・経緯を聞かれた時に使う。"
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "query": {
                        "type": "string",
                        "description": "検索したい内容 (日本語の短いフレーズ。例: 先週の全社売上 / 船橋の出店候補)",
                    },
                },
                "required": ["query"],
            },
        },
        "messages": [
            {"type": "request-start", "content": "ちょっと調べるね。"},
            {"type": "request-failed", "content": "すぐには出てこなかった。あとで調べて送る。"},
        ],
    }


def attach_brain_search(cfg: dict) -> dict:
    """trusted assistant config に brain_search tool + prompt ガイドを付与 (in-place)。

    caller の責務 (main.py 側、source-level test で pin): trusted 分岐のみで呼ぶ。
    tool 定義は secret を含まないので web-config (ブラウザ返却) に載っても露出は増えない。"""
    model = cfg.get("model")
    if not isinstance(model, dict):
        return cfg
    tools = model.setdefault("tools", [])
    if not any((t.get("function") or {}).get("name") == TOOL_NAME for t in tools):
        tools.append(tool_definition())
    msgs = model.get("messages") or []
    if msgs and isinstance(msgs[0], dict) and msgs[0].get("role") == "system":
        if "brain_search" not in (msgs[0].get("content") or ""):
            msgs[0]["content"] = (msgs[0].get("content") or "") + PROMPT_GUIDANCE
    return cfg


# 音声で読み上げ材料にならない markdown ノイズ (URL / 見出し / 強調 / 表罫 / wikilink 括弧)
_MD_NOISE_RE = re.compile(r"https?://\S+|[#*`|]+|\[\[|\]\]")


def _truncate_soft(text: str, limit: int) -> str:
    """limit 超過時、数字の桁の途中で切らない (「1,246,505,」で断ち切ると LLM が
    誤補完して確信を持った誤数字になる = DA)。切断点が数値内なら数値ごと落とす。"""
    if len(text) <= limit:
        return text
    cut = text[:limit]
    while cut and (cut[-1].isdigit() or cut[-1] in ",."):
        cut = cut[:-1]
    return cut


async def search_brain_for_voice(brain, query: str, n_results: int = 6,
                                 max_chars: int = 900) -> str:
    """chroma vector 検索 → 音声会話で咀嚼しやすい短いテキストに決定論整形。

    ★注: wiki collection は personal/ 除外済だが interview/ と clone_visibility:private の
    OWNDAYS 文書は含む (= 海山専用 trusted 経路のみが呼ぶ前提、§1.17 の 6 系統目例外)。
    embedding+chroma を 8s で打ち切り (Vapi 側 timeout より先に graceful に返す)。"""
    import asyncio as _aio
    index = getattr(brain, "index", None)
    if index is None:
        return "検索索引がまだ起動していない。少し待ってもう一度。"
    hits = await _aio.wait_for(
        index.search(query, n_results=n_results, collection="wiki"), timeout=8.0)
    if not hits:
        return "該当する情報は手元の Brain に見つからなかった。"
    parts: list = []
    seen: set = set()
    total = 0
    for h in hits:
        src = h.get("source") or ""
        if src in seen:
            continue
        seen.add(src)
        meta = h.get("metadata") or {}
        title = meta.get("title") or src or "?"
        doc = _MD_NOISE_RE.sub(" ", (h.get("content") or ""))
        doc = re.sub(r"\s+", " ", doc).strip()
        piece = f"■{title}: {_truncate_soft(doc, 240)}"
        parts.append(piece)
        total += len(piece)
        if total > max_chars or len(parts) >= 4:
            break
    return "\n".join(parts)


async def handle_tool_calls(msg: dict, brain) -> dict:
    """message.type="tool-calls" payload を処理して Vapi 応答 dict を返す。
    複数 toolCall・arguments の str/dict 揺れ (flat 形 / OpenAI 入れ子形の公式混在)・
    未知 tool 名に防御的。例外は握って会話を止めない (結果 text でエラーを伝える)。
    results entry の name は spec required (ToolCallResult)。"""
    results = []
    for tc in (msg.get("toolCallList") or []):
        tc_id = tc.get("id") or ""
        fn = tc.get("function") or {}
        name = tc.get("name") or fn.get("name") or ""
        args = tc.get("arguments")
        if args is None:
            args = fn.get("arguments")
        if isinstance(args, str):
            try:
                args = json.loads(args)
            except Exception:
                args = {}
        if not isinstance(args, dict):
            args = {}
        if name != TOOL_NAME:
            results.append({"toolCallId": tc_id, "name": name or TOOL_NAME,
                            "result": f"unknown tool: {name}"})
            continue
        query = str(args.get("query") or "").strip()[:200]
        if not query:
            results.append({"toolCallId": tc_id, "name": TOOL_NAME,
                            "result": "検索語が空だった。何を調べたいかもう一度。"})
            continue
        try:
            text = await search_brain_for_voice(brain, query)
        except Exception as e:
            logger.warning(f"[voice-tools] brain_search failed q={query!r}: {e}")
            text = "検索でエラーが出た。少し後にもう一度試して。"
        results.append({"toolCallId": tc_id, "name": TOOL_NAME, "result": text})
    return {"results": results}
