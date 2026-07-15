#!/usr/bin/env python3
"""scripts/ai_advisor.py — OWNDAYS 事業向け「AI 活用トレンド & 提言」エージェント.

★2026-06-20 海山指示「最新 AI トレンドの取り込み・提案が弱い / うみやまAI から AI への提言を」。
ai_research_agent(= PJ 自身の改善提案)の **ビジネス版**: 外部 AI 進化を OWNDAYS 事業
(店舗 / 検眼 / レンズ加工 / EC / 接客 / 在庫 / BI)の文脈で咀嚼し、
(1) wiki 化(= うみやまAI が引用・先回り提言できる)+ (2) 週次で海山へ提言 push する。

インフラは全再利用(新規依存なし):
- fetchers: ai_research_agent.fetch_arxiv_recent / _hn_ai_stories / _vendor_news(公開 API・キー不要)
- LLM: clone_improve_lib.call_llm(smart=Opus)/ push: clone_improve_lib.line_push(cron 同期前提=本 agent は cron)
- wiki: build_analysis_wiki(§1.7 安全な決定論 write、ANALYSIS_DIR は repo 相対=host で安全)
        → data/brain/wiki/analysis/ai-trends-owndays.md(public・static-factual)
        → main.py _watch_wiki_changes(§1.5 安全な in-process 直列 index)が次サイクルで index

捏造禁止(cross-check 反映): トレンドは出所 URL 付き事実、活用案は『仮説 / 要検証』明示、
ROI・%・時間短縮は概算で断定しない(public wiki = static-factual、数値断定は本文で禁止)。

実行(host cron、ai_research と同じ script-mode): python3 scripts/ai_advisor.py [--dry-run] [--no-push] [--no-wiki]
"""
from __future__ import annotations

import argparse
import asyncio
import logging
import sys
from datetime import date
from pathlib import Path

import httpx

sys.path.insert(0, str(Path(__file__).resolve().parent))   # scripts/ を path に(sibling import)

from ai_research_agent import (  # noqa: E402  公開 fetcher を再利用(重複実装しない)
    fetch_arxiv_recent, fetch_hn_ai_stories, fetch_vendor_news,
)
from build_analysis_wiki import build_analysis_wiki  # noqa: E402

try:
    from clone_improve_lib import call_llm, line_push  # noqa: E402
except Exception:  # テスト・最小環境では stub(注入で差し替え)
    async def call_llm(*a, **k):  # type: ignore
        raise RuntimeError("call_llm unavailable")

    def line_push(text: str) -> bool:  # type: ignore
        return False

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger("ai_advisor")

WIKI_PJ_ID = "ai-trends-owndays"

OWNDAYS_AI_CONTEXT = """# OWNDAYS — 事業概況(= AI 活用提案の relevance 判定の前提)
OWNDAYS = グローバル眼鏡 SPA(日本 + 海外、製造小売)。主要 value chain と AI 接点:
- 店舗接客 / 検眼(オプトメトリー)/ レンズ加工 / フィッティング / 店頭体験
- EC / オンライン試着 / CRM / 会員 / 販促 / レコメンド
- 在庫 / 物流 / 店舗オペ / シフト / 人材教育 / 採用
- 経営: 売上 / 客単価 / 出店(空白地分析あり)/ 社内 AI 浸透(= 海山のミッション)
判定: 眼鏡 SPA・リテール・接客・検眼・EC の現場で『今のモデル/ツールで小さく試せる』打ち手を重視。
純粋な研究段階の話は watch list へ回す。"""

ADVISORY_PROMPT = """あなたは OWNDAYS の AI 活用アドバイザー。直近の世界 AI 進化を、
OWNDAYS の事業現場に『どう活かすか』へ翻訳する。社内システムの改善ではなく、
**OWNDAYS というビジネス**(店舗 / 検眼 / EC / 接客 / 在庫 / BI)の AI 活用提言を出す。

{owndays_context}

# 集約された raw 情報(直近)
## arxiv 論文
{arxiv_section}
## Hacker News(AI 関連 top)
{hn_section}
## Vendor news(Anthropic / OpenAI / Google AI)
{vendor_section}

# 任務
1. 上記から OWNDAYS 事業に **実際に効きうる** 進化を 4-8 件選別(generic / 無関係 / 純研究は捨てる)。
2. 各 item:
   - title(1 行)
   - 何が新しいか(可能なら出所 URL を併記。無ければ書かない)
   - OWNDAYS でどこに効くか(店舗 / 検眼 / EC / 接客 / 在庫 / BI のどれか具体的に)
   - 試せる打ち手(= 小さく pilot できる形。例「店舗の◯◯を△△で試す」)
   - 確度(高 / 中 / 低)+ 着手難易度(小 / 中 / 大)
3. 「直近 90 日で pilot 価値ある top 3」を urgent に。無理に 3 件埋めない(0-3 件で良い)。
4. 「watch list(今は様子見)」を別に。

# 絶対制約(= 捏造禁止。破ると有害。うみやまAI がこれを社員に public 引用する前提。cross-check Devil's Advocate 反映)
- 定量数値(ROI / % / 時間短縮 / コスト)は **原則載せない**。載せてよいのは **実際に出所 URL から引用できる数字だけ**(その場合は URL を必ず併記)。OWNDAYS への効果は **定性で**『〜できる可能性(要検証)』と書く。**自分で概算・推定値を作らない**(捏造した数値が wiki 経由で『CEO 公認の事実』に化けるのを防ぐ)。
- ベンダーやモデルの能力を **でっち上げない**。「できる」と書くのは出所 URL があるか、現行モデルで一般に可能なものだけ。怪しければ「要・外部確認」。
- OWNDAYS の現場詳細は知らない前提。断定でなく『option / たたき台』として書く。最終判断は海山。

# 出力(markdown 本文のみ。frontmatter / preamble / 後語は禁止)
## 直近 pilot top 3
### 1. <title>
- 新規性: <... 出所 URL があれば>
- OWNDAYS 接点: <現場>
- 試せる打ち手: <小さく pilot する形>
- 確度: 中 / 難易度: 小

## watch list(様子見)
- <title>: <1 行コメント>
"""


async def synthesize(papers: list[dict], stories: list[dict], vendors: list[dict],
                     llm=None) -> str:
    """raw source を OWNDAYS ビジネスの AI 活用提言へ統合(smart=Opus)。llm 注入でテスト可。"""
    llm = llm or call_llm
    arxiv = "\n".join(
        f"- [{p.get('category', '?')}] {p.get('title', '')}: {p.get('summary', '')[:200]} {p.get('link', '')}"
        for p in papers[:25]) or "(none)"
    hn = "\n".join(
        f"- {s.get('title', '')} (score {s.get('score', 0)}) {s.get('url', '')}"
        for s in stories[:20]) or "(none)"
    vendor = "\n".join(
        f"- [{v.get('vendor', '?')}] {v.get('title', '')}" for v in vendors[:25]) or "(none)"
    prompt = ADVISORY_PROMPT.format(
        owndays_context=OWNDAYS_AI_CONTEXT,
        arxiv_section=arxiv[:7000], hn_section=hn[:3000], vendor_section=vendor[:3000])
    out = await llm(prompt, model="smart", max_tokens=4000, temperature=0.4)
    return (out or "").strip()


def emit_wiki(body: str, sources: list[str]) -> Path:
    """提言を private・static-factual な analysis wiki に決定論的 write(海山/admin 限定、clone 非引用)。

    ★2026-06-21 世界基準評価(security): web 由来 content を無 fence で LLM 合成し public clone 引用可に
    する設計は poisoning 面(攻撃者が arxiv/HN/vendor の見出しに注入→社員に CEO 公認の偽提言)。
    content-validator(数値は出所URL必須・埋込指示の無害化)が出来るまで private に降格。週次 push は維持。
    """
    return build_analysis_wiki(
        pj_id=WIKI_PJ_ID,
        title="OWNDAYS AI 活用トレンドと提言",
        overview=(f"（{date.today().isoformat()} 時点)直近の世界 AI 進化を OWNDAYS 事業"
                  "(店舗 / 検眼 / EC / 接客 / 在庫 / BI)の文脈で咀嚼した提言。"
                  "トレンドは出所付きの事実、活用案は『仮説・要検証の option』。"
                  "ROI 等の数値は載せない方針(出所付き引用を除く)。最新性は valid_until で失効。最終判断は海山。"),
        sections=[("提言(OWNDAYS AI 活用)", body)],
        pj_class="static-factual",
        sources=sources or ["arxiv", "hacker-news", "vendor-news"],
        tags=["ai", "trends", "advisory"],
        visibility="private",   # ★2026-06-21 security: web 由来合成の poisoning 面につき private(clone 非引用)
        confidence="medium",
        valid_days=14,            # 2 週間で自動失効(= cron 停滞時に古いトレンドを「最新」と誤提示しない)
    )


def build_push(body: str) -> str:
    """LINE push 用の短い要約(top をそのまま、うみやまAI への誘導付き)。"""
    chunk = body.strip()
    if len(chunk) > 760:
        chunk = chunk[:760].rsplit("\n", 1)[0] + "\n…"
    return ("🤖 OWNDAYS AI 活用提言(週次)\n\n" + chunk +
            "\n\n— 詳細はうみやまAIに「AIで店舗/検眼/EC/接客の何を変えるべき?」と聞けます")


async def run(*, dry_run: bool = False, push: bool = True, write_wiki: bool = True,
              llm=None, push_fn=None) -> dict:
    """fetch → synthesize → (wiki write) → (LINE push)。各 fetch は best-effort。"""
    push_fn = push_fn or line_push
    papers: list[dict] = []
    stories: list[dict] = []
    vendors: list[dict] = []
    async with httpx.AsyncClient(timeout=30.0, headers={"User-Agent": "owndays-ai-advisor/1.0"}) as http:
        for label, coro in (("arxiv", fetch_arxiv_recent(http, max_per_cat=15)),
                            ("hn", fetch_hn_ai_stories(http, max_items=30)),
                            ("vendor", fetch_vendor_news(http))):
            try:
                res = await coro
                if label == "arxiv":
                    papers = res
                elif label == "hn":
                    stories = res
                else:
                    vendors = res
            except Exception as e:
                logger.warning(f"fetch {label} failed: {type(e).__name__}: {e}")

    body = await synthesize(papers, stories, vendors, llm=llm)
    if not body or len(body) < 80:
        logger.warning("advisory synthesis empty/too short → skip")
        return {"ok": False, "reason": "empty_synthesis",
                "src": {"arxiv": len(papers), "hn": len(stories), "vendor": len(vendors)}}

    result = {"ok": True, "chars": len(body),
              "src": {"arxiv": len(papers), "hn": len(stories), "vendor": len(vendors)}}
    if dry_run:
        print(body)
        return {**result, "dry_run": True}
    if write_wiki:
        try:
            result["wiki"] = str(emit_wiki(body, ["arxiv", "hacker-news", "vendor-news"]))
        except Exception as e:
            logger.error(f"emit_wiki failed: {type(e).__name__}: {e}")
            result["wiki_error"] = str(e)[:200]
    if push:
        try:
            result["pushed"] = bool(push_fn(build_push(body)))
        except Exception as e:
            logger.warning(f"line_push failed: {type(e).__name__}: {e}")
            result["pushed"] = False
    return result


def main() -> int:
    ap = argparse.ArgumentParser(description="OWNDAYS AI 活用アドバイザー(週次、host cron)")
    ap.add_argument("--dry-run", action="store_true", help="synthesize して本文を表示(wiki/push しない)")
    ap.add_argument("--no-push", action="store_true")
    ap.add_argument("--no-wiki", action="store_true")
    a = ap.parse_args()
    r = asyncio.run(run(dry_run=a.dry_run, push=not a.no_push, write_wiki=not a.no_wiki))
    print(r)
    return 0 if r.get("ok") else 1


if __name__ == "__main__":
    raise SystemExit(main())
