"""ai_research_agent.py — 世界 AI 進化キャッチアップ + Personal Brain 反映提案 (★2026-05-24 海山指示)

# 役割

週次で:
1. 世界中の AI 進化を主要 source から自動収集 (= arxiv / HN / Anthropic / OpenAI 等)
2. 当 PJ architecture (= retrieval / memory / multi-channel / clone) に照らして関連性判定
3. 具体的な「反映提案」を生成 (= 「Tier 1 memory に X を導入すべき」等)
4. 海山 LINE に digest Push、`/research` で browse 可

# 設計

- 頻度: 週 1 (月曜 09:00 JST、scripts/cron_install.sh で登録)
- Source v0: arxiv cs.AI/cs.CL/cs.LG + Hacker News AI top + Anthropic/OpenAI news
- LLM: smart (Claude Opus 4.8) for relevance + synthesis、PJ context を inject
- Output:
  - data/brain/ai_research/{YYYY-MM-DD}-digest.md (永続 digest)
  - data/brain/ai_research/proposals.jsonl (蓄積 proposals)
  - LINE Push (= 1500 字以内 sumamry)
- Cost: ~$0.50-1/run = ~$2-4/月

# usage

```
python3 scripts/ai_research_agent.py             # 通常実行 (= 1 週間分集計)
python3 scripts/ai_research_agent.py --days 14   # 集計範囲 override
python3 scripts/ai_research_agent.py --dry-run   # LLM call なし、source fetch + 件数のみ
python3 scripts/ai_research_agent.py --no-push   # LINE Push 抑制
```

# 反映提案の品質

LLM に **当 PJ architecture 概要を毎回 inject**:
- 当 PJ: Personal Brain (= OWNDAYS CEO 海山の LINE bot + 自己複製基盤)
- 主要 component: brain_wiki (retrieval), clone_memory (per-user), clone_group_context (per-group),
  Cohere Rerank, contextual chunks, sleep_time_agent, clone_audit, usage_analytics
- 現 Phase 1 target: 月 1,000 → 10,000 query, heavy user 100 × 20/月

これに基づき LLM が「**当 PJ に reflect 価値ある進化か**」を判定、wasteful な generic info を排除。
"""
from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
import re
import sys
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Optional

import httpx

logger = logging.getLogger("ai_research_agent")
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")

JST = timezone(timedelta(hours=9))
APP_ROOT = Path(os.getenv("BRAIN_APP_ROOT", "/app"))
RESEARCH_DIR = APP_ROOT / "data" / "brain" / "ai_research"
PROPOSALS_FILE = RESEARCH_DIR / "proposals.jsonl"

# scripts/ 配下 module 取込
sys.path.insert(0, str(Path(__file__).resolve().parent))
try:
    from clone_improve_lib import call_llm, line_push, line_push_digest, supervisor_model  # type: ignore
except Exception as e:
    logger.warning(f"clone_improve_lib import failed: {e}")

    def call_llm(*args, **kwargs):  # type: ignore
        raise RuntimeError("call_llm unavailable")

    def line_push(text: str) -> bool:  # type: ignore
        logger.info(f"[LINE PUSH stub]\n{text[:200]}...")
        return False

    def supervisor_model() -> str:  # type: ignore
        return os.getenv("SUPERVISOR_MODEL", "supervisor")

try:
    from bot_events import log_bot_event  # type: ignore
except Exception:
    def log_bot_event(*args, **kwargs):  # type: ignore
        pass


# ─── Source 1: arxiv RSS ──────────────────────────────────
ARXIV_CATEGORIES = ["cs.AI", "cs.CL", "cs.LG", "cs.IR"]
ARXIV_RSS_BASE = "https://export.arxiv.org/rss/"


async def fetch_arxiv_recent(http: httpx.AsyncClient, max_per_cat: int = 15) -> list[dict]:
    """arxiv RSS から各 category 直近 N 件取得。

    Returns:
        [{"title": str, "authors": str, "summary": str, "link": str, "category": str, "pub_date": str}, ...]
    """
    papers = []
    for cat in ARXIV_CATEGORIES:
        url = f"{ARXIV_RSS_BASE}{cat}"
        try:
            resp = await http.get(url, timeout=15.0)
            resp.raise_for_status()
            xml_str = resp.text
        except Exception as e:
            logger.warning(f"arxiv {cat} fetch failed: {e}")
            continue

        try:
            root = ET.fromstring(xml_str)
            # RSS 2.0 で channel/item
            ns = {"atom": "http://www.w3.org/2005/Atom"}
            for item in root.findall(".//item")[:max_per_cat]:
                title = (item.findtext("title") or "").strip()
                desc = (item.findtext("description") or "").strip()
                link = (item.findtext("link") or "").strip()
                # author 取り出し (= dc:creator)
                creator = item.findtext("{http://purl.org/dc/elements/1.1/}creator") or ""
                pub = (item.findtext("pubDate") or "").strip()
                papers.append({
                    "source": "arxiv",
                    "category": cat,
                    "title": title[:200],
                    "authors": creator[:200],
                    "summary": _strip_html(desc)[:800],
                    "link": link,
                    "pub_date": pub,
                })
        except Exception as e:
            logger.warning(f"arxiv {cat} parse failed: {e}")
    logger.info(f"arxiv: {len(papers)} papers fetched")
    return papers


def _strip_html(text: str) -> str:
    """簡易 HTML tag 除去 (= description が HTML 文字列のケース)。"""
    if not text:
        return ""
    return re.sub(r"<[^>]+>", " ", text).replace("&nbsp;", " ").strip()


# ─── Source 2: Hacker News top stories (AI filter) ────────
HN_TOP_URL = "https://hacker-news.firebaseio.com/v0/topstories.json"
HN_ITEM_URL = "https://hacker-news.firebaseio.com/v0/item/{id}.json"

# AI 関連 keyword (= title filter 用)
AI_KEYWORDS = [
    "AI ", " AI", "GPT", "Claude", "Anthropic", "OpenAI", "Gemini", "DeepMind",
    "Llama", "LLM", "Mistral", "Cohere", "Hugging Face", "transformer",
    "fine-tun", "RAG", "retrieval", "embedding", "vector DB", "agent",
    "neural", "deep learning", "machine learning", "Sora", "DALL",
    "Whisper", "TTS", "stable diffusion", "Mid Journey", "multimodal",
    "ChatGPT", "Bard", "Copilot", "Cursor", "Devin", "ML model",
]


async def fetch_hn_ai_stories(http: httpx.AsyncClient, max_items: int = 30) -> list[dict]:
    """Hacker News top stories から AI keyword に hit する記事を取得。"""
    try:
        resp = await http.get(HN_TOP_URL, timeout=10.0)
        resp.raise_for_status()
        top_ids = resp.json()[:200]  # top 200 から filter
    except Exception as e:
        logger.warning(f"HN top fetch failed: {e}")
        return []

    stories = []
    for hn_id in top_ids:
        if len(stories) >= max_items:
            break
        try:
            r = await http.get(HN_ITEM_URL.format(id=hn_id), timeout=10.0)
            r.raise_for_status()
            item = r.json()
            if not item or item.get("type") != "story":
                continue
            title = item.get("title", "") or ""
            # AI keyword filter
            if not any(kw.lower() in title.lower() for kw in AI_KEYWORDS):
                continue
            stories.append({
                "source": "hackernews",
                "title": title[:200],
                "url": item.get("url", "") or f"https://news.ycombinator.com/item?id={hn_id}",
                "score": item.get("score", 0),
                "comments": item.get("descendants", 0),
                "by": item.get("by", ""),
                "time": item.get("time", 0),
            })
        except Exception as e:
            logger.debug(f"HN item {hn_id} fetch failed: {e}")
            continue
    logger.info(f"hacker news: {len(stories)} AI stories filtered")
    return stories


# ─── Source 3: Anthropic / OpenAI news (HTML 検出、軽量) ───
NEWS_PAGES = [
    ("Anthropic", "https://www.anthropic.com/news"),
    ("OpenAI", "https://openai.com/blog"),
    ("Google AI", "https://blog.google/technology/ai/"),
]


async def fetch_vendor_news(http: httpx.AsyncClient) -> list[dict]:
    """各 vendor の news page から 直近 article title を抽出 (= rough、後 LLM 整理)."""
    items = []
    for vendor, url in NEWS_PAGES:
        try:
            resp = await http.get(url, timeout=15.0, follow_redirects=True,
                                  headers={"User-Agent": "Mozilla/5.0 (Personal Brain Research Agent)"})
            if resp.status_code != 200:
                logger.warning(f"{vendor} fetch HTTP {resp.status_code}")
                continue
            html = resp.text
            # 大まかに <h1>-<h4> tag の text を抽出 (= 詳細解析せずに title raw 取得、LLM が後整理)
            titles = re.findall(r"<h[1-4][^>]*>(.*?)</h[1-4]>", html, re.DOTALL | re.IGNORECASE)[:30]
            for t in titles:
                clean = _strip_html(t).strip()
                if len(clean) < 10 or len(clean) > 300:
                    continue
                items.append({
                    "source": vendor.lower(),
                    "vendor": vendor,
                    "title": clean,
                    "url": url,
                })
        except Exception as e:
            logger.warning(f"{vendor} fetch failed: {e}")
            continue
    logger.info(f"vendor news: {len(items)} titles")
    return items


# ─── PJ context (= LLM への inject 用、relevance 判定の前提) ───
PJ_CONTEXT = """# Personal Brain Project — 現在 architecture (= 提案 relevance 判定の前提)

## 概要
OWNDAYS CEO 海山丈司の personal AI 「うみやまAI」。LINE / LINE WORKS Bot + 自己複製基盤。
目標: Phase 1 (社内 200 + 300 名向け Q&A) → Phase 2 (Meet 参加) → Phase 3 (意思決定モード)。
target: 月 1,000 → 10,000 query / heavy user 100 × 20/月。

## 主要 component
- **brain_wiki**: chromadb retrieval, Contextual Retrieval (Anthropic 手法), Cohere Rerank 3.5
- **clone_memory**: per-user 4 section memory (Profile/Ongoing/Facts/Preferences), date metadata 付き
- **clone_group_context**: per-group 4 section, Tier 0 で追加
- **CLONE_PUBLIC_PROMPT**: 暗黙参照解決 instruction (= 「あの件」「先週」 silent resolve), Tier 1
- **clone_audit**: 海山 1-click 評価 UI (○/×/!), production 品質 closed loop center
- **sleep_time_agent**: idle 30s で memory 再整理 (Letta pattern), smart model
- **usage_analytics**: ROI dashboard, /admin/usage
- **bot_uptime_monitor**: component_streak で個別障害検知 + LINE Push
- **drive_ingest**: Google Workspace URL on-demand fetch
- **services/auth**: admin user_id 検証
- **bot_events.jsonl**: 構造化 event log

## LLM
- LiteLLM proxy 経由
- smart = Claude Opus 4.8, smart-gpt = GPT-5.4, fast = GPT-4o, fast-gpt = GPT-5.4-mini
- prompt cache (= Anthropic ephemeral) 階層化

## 既知の Gap (= 提案で埋めたい)
- multi-tenant 化 (= 横展開時)
- Multi-channel adapter pattern (= Phase 2 Meet 参加前)
- A/B test framework 稼働 (= build 済、measurement 未)
- Service Account 化 (= Google OAuth user credential dependency)
- judgment generation (= Phase 3 真の経営判断、現状 retrieval + style mimicry 止まり)
- HA / multi-host (= 現状 Mac Studio 単一)
- load test (= 500 user concurrent 未検証)

## Phase Plan
- Phase 0 (= 来週開始): 海山 + 役員 3-5 名 限定 test
- Phase 1a: 限定 group 1-2 個拡大
- Phase 1b: 本部 200 名
- Phase 1c: 店舗 300 + 海外
- Phase 2: Meet 参加 (= 音声 + 介入判断)
- Phase 3: 意思決定モード (= 不在時代行)
"""


# ─── LLM synthesis (= 関連性 + 提案 生成) ──────────────────
SYNTHESIS_PROMPT = """あなたは Personal Brain Project の AI research 担当エージェント。
直近 1 週間の世界 AI 進化を集約し、当 PJ への **具体的反映提案** を生成する。

{pj_context}

# 集約された raw 情報

## arxiv 論文 (= cs.AI/CL/LG/IR 各 category 直近)
{arxiv_section}

## Hacker News AI top 記事
{hn_section}

## Vendor news (= Anthropic / OpenAI / Google AI)
{vendor_section}

# 任務

1. 上記から **当 PJ に reflect 価値ある進化** を 5-10 件選別 (= generic / 無関係は捨てる)
2. 各 item について:
   - title (= 1 行要約)
   - 何が新しいか (= 50-100 字)
   - 当 PJ のどの component に reflect 可能か (= 具体 module 名)
   - 反映方法 (= 「retrieval pipeline に X を追加」「memory に Y を導入」等、specific に)
   - 推定 impact (high/medium/low) + 推定 effort (= sprint 数)
   - priority (= impact × 当 PJ phase 関連性)
3. 「**直近 1-2 週間で着手すべき top 3**」を urgent section に
4. 「**Phase 2-3 で参考にする watch list**」を別 section に

# 出力 (= markdown only、frontmatter なし)

```markdown
# AI Research Digest — YYYY-MM-DD (週次)

## ⚡ 直近 着手 top 3

### 1. <title>
- **新規性**: <50-100 字>
- **PJ 反映**: <component> に <方法>
- **impact**: high / **effort**: 1 sprint
- **理由**: <なぜ top に置くか>

### 2. ...

### 3. ...

## 📚 着手保留 (= 関連性あるが Phase 1 後)

### 4. <title>
- **新規性**: ...
- **PJ 反映**: ...
- **impact**: medium / **effort**: 2 sprint

### 5-10 ...

## 👀 Watch list (= Phase 2-3 参考、即 action 無し)

- <title>: <1 行コメント>
- <title>: ...

## 📊 Source 統計

- arxiv: N 件 / hacker news: M 件 / vendor news: K 件
- 関連性ある items: 抽出 N 件
```

# 制約

- 出力は **markdown 本文のみ** (= 解説 / preamble / 後語禁止)
- 5-10 件に絞る、generic AI news は除外
- 反映方法は **当 PJ の具体 module 名 を必ず含む**
- impact/effort は感覚値で OK、ただし高めに見積もらない (= 確証薄ければ medium / 2 sprint)
- urgent top 3 は **本当に直近着手価値ある** items のみ、無理に 3 件埋めない (= 0-3 件 OK)
"""


async def synthesize_digest(
    papers: list[dict],
    stories: list[dict],
    vendor_items: list[dict],
    today: str,
) -> str:
    """LLM (smart) で raw source から digest 生成。"""
    arxiv_lines = []
    for p in papers[:30]:
        arxiv_lines.append(
            f"- [{p.get('category', '?')}] {p.get('title', '')}\n"
            f"  authors: {p.get('authors', '')[:100]}\n"
            f"  summary: {p.get('summary', '')[:300]}\n"
            f"  link: {p.get('link', '')}"
        )
    arxiv_section = "\n".join(arxiv_lines) or "(no papers)"

    hn_lines = []
    for s in stories[:20]:
        hn_lines.append(
            f"- {s.get('title', '')}  (score {s.get('score', 0)}, "
            f"comments {s.get('comments', 0)})\n  {s.get('url', '')}"
        )
    hn_section = "\n".join(hn_lines) or "(no stories)"

    vendor_lines = []
    for v in vendor_items[:30]:
        vendor_lines.append(f"- [{v.get('vendor', '?')}] {v.get('title', '')}")
    vendor_section = "\n".join(vendor_lines) or "(no vendor news)"

    prompt = SYNTHESIS_PROMPT.format(
        pj_context=PJ_CONTEXT,
        arxiv_section=arxiv_section[:8000],
        hn_section=hn_section[:3000],
        vendor_section=vendor_section[:3000],
    )

    logger.info(f"LLM synthesis: arxiv={len(papers)} hn={len(stories)} vendor={len(vendor_items)}")
    # ★2026-07-10 監督者層 = Fable 5 (litellm supervisor、fallback: smart→smart-fallback)
    digest = await call_llm(prompt, model=supervisor_model(), max_tokens=10000, temperature=None)
    return (digest or "").strip()


# ─── 保存 + 通知 ──────────────────────────────────────────
def save_digest(digest: str, today: str) -> Path:
    """digest を file に保存."""
    RESEARCH_DIR.mkdir(parents=True, exist_ok=True)
    p = RESEARCH_DIR / f"{today}-digest.md"
    p.write_text(digest, encoding="utf-8")
    return p


def extract_proposals(digest: str, today: str) -> list[dict]:
    """digest から ### で始まる proposal item を抽出、proposals.jsonl に追記."""
    items = []
    # markdown の ### title 単位で分解
    sections = re.split(r"^### (\d+)\.\s*", digest, flags=re.MULTILINE)
    if len(sections) < 3:
        return items
    # split で [前置, "1", "content1", "2", "content2", ...] になる
    for i in range(1, len(sections), 2):
        if i + 1 >= len(sections):
            break
        num = sections[i]
        body = sections[i + 1]
        # title (= 最初の行)
        first_line = body.split("\n", 1)[0].strip()
        rest = body.split("\n", 1)[1] if "\n" in body else ""
        items.append({
            "id": f"{today}_p{num.zfill(2)}",
            "ts": datetime.now(JST).isoformat(),
            "title": first_line[:200],
            "body": rest[:2000].strip(),
            "status": "pending",
            "source_digest": f"{today}-digest.md",
        })

    if items:
        RESEARCH_DIR.mkdir(parents=True, exist_ok=True)
        with PROPOSALS_FILE.open("a", encoding="utf-8") as f:
            for it in items:
                f.write(json.dumps(it, ensure_ascii=False) + "\n")
    return items


def build_push_summary(digest: str, today: str, n_proposals: int) -> str:
    """LINE Push 用 summary (= 1500 字以内、urgent top 3 を含む).

    ## ⚡ 開始から **次の ## (= ### じゃない、## 単体)** までを抽出する。
    re.MULTILINE で行頭 ## のみ delimiter 扱い。
    """
    # urgent top 3 section だけ抽出 (= 行頭 ## delimiter、### は中身として保持)
    m = re.search(
        r"^##\s*⚡[\s\S]*?(?=^##\s(?!\#)|\Z)",
        digest,
        re.MULTILINE,
    )
    urgent_section = (m.group(0) if m else "")[:1200]

    return (
        f"🔬 AI Research Digest ({today})\n"
        f"━━━━━━━━━━━━━━━━━━━━\n"
        f"{urgent_section}\n"
        f"\n"
        f"... (digest 全文 {n_proposals} 件、`/research` で閲覧)"
    )


# ─── main orchestration ───────────────────────────────────
async def run(
    days: int = 7,
    dry_run: bool = False,
    no_push: bool = False,
) -> dict:
    """週次 research run."""
    today = datetime.now(JST).strftime("%Y-%m-%d")
    started = datetime.now(JST)

    async with httpx.AsyncClient(timeout=30.0) as http:
        papers = await fetch_arxiv_recent(http, max_per_cat=15)
        stories = await fetch_hn_ai_stories(http, max_items=30)
        vendors = await fetch_vendor_news(http)

    summary = {
        "started_at": started.isoformat(),
        "today": today,
        "arxiv_count": len(papers),
        "hn_count": len(stories),
        "vendor_count": len(vendors),
    }

    if dry_run:
        logger.info(f"DRY RUN: {summary}")
        summary["dry_run"] = True
        return summary

    # LLM synthesis
    try:
        digest = await synthesize_digest(papers, stories, vendors, today)
    except Exception as e:
        logger.exception(f"LLM synthesis failed: {e}")
        log_bot_event("ai_research", "turn_failed",
                      error_class=type(e).__name__, error_msg=str(e)[:200])
        return {**summary, "error": str(e)}

    if not digest:
        logger.warning("LLM returned empty digest")
        return {**summary, "error": "empty digest"}

    # 保存
    digest_path = save_digest(digest, today)
    proposals = extract_proposals(digest, today)

    summary["digest_path"] = str(digest_path)
    summary["n_proposals"] = len(proposals)
    summary["chars"] = len(digest)

    # LINE Push
    if not no_push:
        try:
            push_text = build_push_summary(digest, today, len(proposals))
            ok = line_push_digest(push_text, "AI研究")
            summary["line_pushed"] = ok
        except Exception as e:
            logger.warning(f"line_push failed: {e}")
            summary["line_pushed"] = False

    log_bot_event("ai_research", "turn_finished",
                  digest_chars=len(digest), n_proposals=len(proposals))
    logger.info(f"ai_research done: digest={digest_path}, proposals={len(proposals)}")
    return summary


def main():
    parser = argparse.ArgumentParser(description="AI Research Agent (週次)")
    parser.add_argument("--days", type=int, default=7, help="集計対象日数 (default 7)")
    parser.add_argument("--dry-run", action="store_true",
                        help="LLM call skip、source fetch + 件数のみ")
    parser.add_argument("--no-push", action="store_true",
                        help="LINE Push 抑制 (= debug 用)")
    args = parser.parse_args()

    result = asyncio.run(run(
        days=args.days, dry_run=args.dry_run, no_push=args.no_push,
    ))
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
