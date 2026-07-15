"""
mcp_brain_server.py — Personal Brain MCP Server

Claude Code / Claude Desktop から直接 Brain Wiki、Google Calendar、
Gmail、Google Drive にアクセスするための MCP サーバー。

起動:
  python mcp_brain_server.py

Claude Code設定 (~/.claude/claude_code_config.json):
  {
    "mcpServers": {
      "brain": {
        "command": "python3",
        "args": ["/Users/brain/brain-agent/mcp_brain_server.py"]
      }
    }
  }
"""

import asyncio
import json
import logging
import os
import sys
from datetime import datetime, date, timedelta
from pathlib import Path

# MCP SDK
from mcp.server import Server
from mcp.server.stdio import stdio_server
from mcp.types import Tool, TextContent

logging.basicConfig(level=logging.INFO, stream=sys.stderr)
logger = logging.getLogger(__name__)

# ─── パス設定 ───
BRAIN_ROOT = Path("/Users/brain/brain-agent/data/brain")
WIKI_DIR = BRAIN_ROOT / "wiki"
RAW_DIR = BRAIN_ROOT / "raw"

# ─── LiteLLM設定 (★平文 hardcode 禁止 2026-05-23 LEE レビュー §3.1: env 経由のみ) ───
LITELLM_URL = os.getenv("LITELLM_URL", "http://localhost:4000")
LITELLM_KEY = os.getenv("LITELLM_MASTER_KEY", "")

server = Server("brain")


# ─── ツール定義 ───

@server.list_tools()
async def list_tools():
    return [
        Tool(
            name="brain_search",
            description="Brain Wikiをベクトル検索。過去の会話、知識、人物情報、方針、意思決定を検索。",
            inputSchema={
                "type": "object",
                "properties": {
                    "query": {
                        "type": "string",
                        "description": "検索クエリ（日本語OK）",
                    },
                    "max_results": {
                        "type": "integer",
                        "description": "最大結果数（デフォルト: 5）",
                        "default": 5,
                    },
                },
                "required": ["query"],
            },
        ),
        Tool(
            name="brain_wiki_read",
            description="Brain Wikiの特定ページを読む。identity, style, thinking, knowledge/*, people/*, projects/*, decisions/* 等。",
            inputSchema={
                "type": "object",
                "properties": {
                    "path": {
                        "type": "string",
                        "description": "Wikiファイルパス（例: identity.md, people/owndays-team.md）",
                    },
                },
                "required": ["path"],
            },
        ),
        Tool(
            name="brain_wiki_list",
            description="Brain Wikiの全ページ一覧を取得。どんな知識があるか把握する。",
            inputSchema={
                "type": "object",
                "properties": {},
            },
        ),
        Tool(
            name="calendar_today",
            description="Google Calendarから予定を取得。今日、明日、今週の予定を確認。",
            inputSchema={
                "type": "object",
                "properties": {
                    "days": {
                        "type": "integer",
                        "description": "取得日数（デフォルト: 1=今日のみ）",
                        "default": 1,
                    },
                },
            },
        ),
        Tool(
            name="gmail_recent",
            description="Gmailから直近のメールを取得。スパム・プロモーション除外済み。",
            inputSchema={
                "type": "object",
                "properties": {
                    "days": {
                        "type": "integer",
                        "description": "過去何日分（デフォルト: 1）",
                        "default": 1,
                    },
                    "max_emails": {
                        "type": "integer",
                        "description": "最大件数（デフォルト: 10）",
                        "default": 10,
                    },
                },
            },
        ),
        Tool(
            name="drive_search",
            description="Google Driveでファイルを検索し、中身を読む。PL、AOP、企画書、報告書等。",
            inputSchema={
                "type": "object",
                "properties": {
                    "query": {
                        "type": "string",
                        "description": "検索キーワード（例: FY27 AOP, 棚卸, SUMMIT企画書）",
                    },
                    "read_content": {
                        "type": "boolean",
                        "description": "ファイルの中身を読むか（デフォルト: true）",
                        "default": True,
                    },
                    "max_files": {
                        "type": "integer",
                        "description": "最大ファイル数（デフォルト: 3）",
                        "default": 3,
                    },
                },
                "required": ["query"],
            },
        ),
        Tool(
            name="brain_raw_conversations",
            description="生の会話ログを読む。日付指定で特定日の全会話を取得。",
            inputSchema={
                "type": "object",
                "properties": {
                    "date": {
                        "type": "string",
                        "description": "日付（YYYY-MM-DD形式、デフォルト: 今日）",
                    },
                },
            },
        ),
    ]


@server.call_tool()
async def call_tool(name: str, arguments: dict):
    try:
        if name == "brain_search":
            return await _brain_search(arguments)
        elif name == "brain_wiki_read":
            return await _brain_wiki_read(arguments)
        elif name == "brain_wiki_list":
            return await _brain_wiki_list(arguments)
        elif name == "calendar_today":
            return await _calendar_today(arguments)
        elif name == "gmail_recent":
            return await _gmail_recent(arguments)
        elif name == "drive_search":
            return await _drive_search(arguments)
        elif name == "brain_raw_conversations":
            return await _brain_raw_conversations(arguments)
        else:
            return [TextContent(type="text", text=f"Unknown tool: {name}")]
    except Exception as e:
        logger.error(f"Tool {name} error: {e}")
        return [TextContent(type="text", text=f"Error: {e}")]


# ─── ツール実装 ───

async def _brain_search(args: dict):
    """ベクトル検索"""
    query = args["query"]
    max_results = args.get("max_results", 5)

    import httpx
    async with httpx.AsyncClient(timeout=15.0) as http:
        # Embedding取得
        embed_resp = await http.post(
            f"{LITELLM_URL}/v1/embeddings",
            headers={"Authorization": f"Bearer {LITELLM_KEY}"},
            json={"model": "text-embedding-3-small", "input": query},
        )
        embed_resp.raise_for_status()
        query_embedding = embed_resp.json()["data"][0]["embedding"]

    # ChromaDB検索
    import chromadb
    client = chromadb.PersistentClient(path="/Users/brain/brain-agent/chroma_data")
    try:
        collection = client.get_collection("brain_wiki")
    except Exception:
        return [TextContent(type="text", text="Brain Wikiインデックスが未構築です。")]

    results = collection.query(
        query_embeddings=[query_embedding],
        n_results=max_results,
    )

    if not results["documents"][0]:
        return [TextContent(type="text", text=f"「{query}」に関する情報は見つかりませんでした。")]

    # ★2026-07-03 (v3 ADR DA R6 cross-check SF3): read/list と同じ深層 private path 防御 +
    #   private visibility 除外。現状 collection 名 "brain_wiki" は実索引 ("wiki") と不一致で
    #   dead code だが、名前を"修正"した瞬間に interview/private chunk が流れる foot-gun を先に塞ぐ。
    from brain_wiki_helpers.domain import is_deep_private_rel
    pairs = [
        (doc, meta) for doc, meta in
        zip(results["documents"][0], results["metadatas"][0])
        if not is_deep_private_rel((meta or {}).get("file") or (meta or {}).get("source") or "")
        and (meta or {}).get("clone_visibility") != "private"
    ]
    if not pairs:
        return [TextContent(type="text", text=f"「{query}」に関する情報は見つかりませんでした。")]

    output = f"## Brain Wiki検索結果: 「{query}」\n\n"
    for i, (doc, meta) in enumerate(pairs):
        source = meta.get("source") or meta.get("file") or "unknown"
        output += f"### [{i+1}] {source}\n{doc}\n\n"

    return [TextContent(type="text", text=output)]


async def _brain_wiki_read(args: dict):
    """Wikiページ読み込み"""
    path = args["path"]
    filepath = WIKI_DIR / path
    if not filepath.exists():
        # サブディレクトリも探す
        candidates = list(WIKI_DIR.rglob(path))
        if not candidates:
            candidates = list(WIKI_DIR.rglob(f"*{path}*"))
        if candidates:
            filepath = candidates[0]
        else:
            return [TextContent(type="text", text=f"ファイル未発見: {path}\n\n利用可能: brain_wiki_list で一覧を確認してください。")]

    # ★2026-06-28 personal ドメイン分離: wiki/personal/ (非OWNDAYS) は MCP 経由で渡さない
    #   (Fact-checker/DA cross-check: brain_wiki_read は visibility 無 filter で外部 client から
    #   path 指定で読めてしまう)。海山の個人 PJ/投資は LINE の /personal モード専用。
    #   併せて WIKI_DIR 外への path traversal も fail-safe で拒否。
    # ★2026-07-03 (v3 ADR DA R6): interview/ (人格深層) も path 防御に統合 = is_deep_private_rel。
    try:
        rel = filepath.resolve().relative_to(WIKI_DIR.resolve())
    except Exception:
        return [TextContent(type="text", text=f"不正なパスです: {path}")]
    from brain_wiki_helpers.domain import is_deep_private_rel
    if is_deep_private_rel(rel):
        return [TextContent(type="text", text="このページは深層 private (personal/ = 非OWNDAYS、interview/ = 人格深層) のため MCP 経由では参照できません。personal は LINE の /personal モード、interview は海山専用経路 (/mcp/brain・音声アラインメント) を使ってください。")]
    content = filepath.read_text(encoding="utf-8")
    return [TextContent(type="text", text=f"## Wiki: {rel}\n\n{content}")]


async def _brain_wiki_list(args: dict):
    """Wiki一覧"""
    if not WIKI_DIR.exists():
        return [TextContent(type="text", text="Wiki ディレクトリが見つかりません。")]

    files = sorted(WIKI_DIR.rglob("*.md"))
    output = "## Brain Wiki ページ一覧\n\n"
    listed = 0
    from brain_wiki_helpers.domain import is_deep_private_rel
    for f in files:
        rel = f.relative_to(WIKI_DIR)
        # ★2026-06-28 personal / ★2026-07-03 interview (v3 ADR DA R6): 深層 private は一覧に
        #   出さない (存在自体を MCP に見せない)
        if is_deep_private_rel(rel):
            continue
        size = f.stat().st_size
        mod = datetime.fromtimestamp(f.stat().st_mtime).strftime("%Y-%m-%d %H:%M")
        output += f"- {rel} ({size}B, {mod})\n"
        listed += 1

    output += f"\n合計: {listed} ページ"
    return [TextContent(type="text", text=output)]


async def _calendar_today(args: dict):
    """カレンダー取得"""
    days = args.get("days", 1)

    sys.path.insert(0, "/Users/brain/brain-agent")
    from google_sync import get_credentials, sync_calendar

    creds = get_credentials()
    events = sync_calendar(creds, days=days, dry_run=True)

    if not events:
        return [TextContent(type="text", text="予定はありません。")]

    output = f"## Google Calendar（{days}日分）\n\n"
    for ev in events:
        t = ev["start"][11:16] if "T" in ev["start"] else "終日"
        line = f"- [{t}] {ev['summary']}"
        if ev.get("location"):
            line += f" @{ev['location']}"
        if ev.get("attendees"):
            line += f" ({', '.join(ev['attendees'][:5])})"
        if ev.get("attachments"):
            line += f" [添付: {len(ev['attachments'])}件]"
        output += line + "\n"

    return [TextContent(type="text", text=output)]


async def _gmail_recent(args: dict):
    """Gmail取得"""
    days = args.get("days", 1)
    max_emails = args.get("max_emails", 10)

    sys.path.insert(0, "/Users/brain/brain-agent")
    from google_sync import get_credentials, sync_gmail

    creds = get_credentials()
    emails = sync_gmail(creds, days=days, max_emails=max_emails, dry_run=True)

    if not emails:
        return [TextContent(type="text", text="新着メールはありません。")]

    output = f"## Gmail（過去{days}日, {len(emails)}件）\n\n"
    for em in emails:
        unread = "*" if em.get("unread") else " "
        sender = em["from"].split("<")[0].strip().strip('"')
        output += f"{unread} **{sender}** | {em['subject']}\n"
        if em.get("snippet"):
            output += f"  {em['snippet'][:100]}\n"
        output += "\n"

    return [TextContent(type="text", text=output)]


async def _drive_search(args: dict):
    """Drive検索+内容読み込み.

    ★2026-05-26 海山指示「給与・人事評価等の機密情報はアクセスできない機能」 強化:
    - gdrive_sync.DEFAULT_EXCLUDE_PATTERN (= 人事評価/給与/採用/個人情報/健康/懲戒/credentials)
      を post-hoc filter で適用
    - 親フォルダ名 check も合わせて適用 (= 「給与」 folder 配下を全 block)
    - Drive API `q` field に `not name contains '...'` 主要 keyword を注入 (= server-side 効率化)
    """
    query = args["query"]
    read_content = args.get("read_content", True)
    max_files = args.get("max_files", 3)

    sys.path.insert(0, "/Users/brain/brain-agent")
    from google_sync import get_credentials, _download_and_extract
    from gdrive_sync import is_confidential_file, build_drive_exclude_clause
    from googleapiclient.discovery import build as gbuild

    creds = get_credentials()
    service = gbuild("drive", "v3", credentials=creds)

    # ★ Drive API server-side exclude 注入
    exclude_clause = build_drive_exclude_clause()

    # ★ 親フォルダ名 check 用 cache + filter helper
    parent_name_cache: dict = {}

    def _filter_confidential(file_list: list) -> list:
        filtered = []
        for f in file_list:
            is_conf, reason = is_confidential_file(f, drive_service=service, parent_name_cache=parent_name_cache)
            if is_conf:
                continue
            filtered.append(f)
        return filtered

    FIELDS = "files(id, name, mimeType, modifiedTime, webViewLink, parents)"

    # ★ Drive API call ラッパー (= 400 BadRequest 時に exclude_clause を外して再試行)
    # corpora="allDrives" で shared drive も対象、機密 filter の意味を実効化。
    def _list_with_fallback(q_base: str, page_size: int) -> list:
        common_kw = dict(
            pageSize=page_size,
            fields=FIELDS,
            orderBy="modifiedTime desc",
            corpora="allDrives",
            supportsAllDrives=True,
            includeItemsFromAllDrives=True,
        )
        try:
            r = service.files().list(q=f"{q_base} and {exclude_clause}", **common_kw).execute()
            return r.get("files", [])
        except Exception:
            try:
                r = service.files().list(q=q_base, **common_kw).execute()
                return r.get("files", [])
            except Exception:
                return []

    # ファイル名検索
    raw = _list_with_fallback(f"name contains '{query}' and trashed = false", max_files * 2)
    files = _filter_confidential(raw)[:max_files]

    # 全文検索フォールバック
    if not files:
        raw = _list_with_fallback(f"fullText contains '{query}' and trashed = false", max_files * 2)
        files = _filter_confidential(raw)[:max_files]

    if not files:
        return [TextContent(type="text", text=f"「{query}」に該当するファイルは見つかりませんでした (= 機密情報 filter で除外された可能性あり)。")]

    output = f"## Drive検索結果: 「{query}」\n\n"
    for f in files:
        mod = f.get("modifiedTime", "")[:10]
        link = f.get("webViewLink", "")
        output += f"- **{f['name']}** ({mod}) {link}\n"

    if read_content:
        for f in files[:2]:
            try:
                text = _download_and_extract(
                    service, f["id"], f["name"], f["mimeType"]
                )
                if text:
                    output += f"\n### ファイル内容: {f['name']}\n```\n{text[:15000]}\n```\n"
                    if len(text) > 15000:
                        output += f"（以下省略、全{len(text)}文字）\n"
            except Exception as e:
                output += f"\n### {f['name']}: 読み込みエラー ({e})\n"

    return [TextContent(type="text", text=output)]


async def _brain_raw_conversations(args: dict):
    """生の会話ログ"""
    target_date = args.get("date", date.today().isoformat())
    conv_dir = RAW_DIR / "conversations"

    filepath = conv_dir / f"{target_date}.md"
    if not filepath.exists():
        # 直近のファイルを探す
        files = sorted(conv_dir.glob("*.md"), reverse=True)
        available = [f.stem for f in files[:5]]
        return [TextContent(
            type="text",
            text=f"{target_date}の会話ログはありません。\n\n直近のログ: {', '.join(available)}"
        )]

    content = filepath.read_text(encoding="utf-8")
    return [TextContent(type="text", text=f"## 会話ログ: {target_date}\n\n{content}")]


# ─── 起動 ───
async def main():
    logger.info("Brain MCP Server starting...")
    async with stdio_server() as (read_stream, write_stream):
        await server.run(read_stream, write_stream, server.create_initialization_options())


if __name__ == "__main__":
    asyncio.run(main())
