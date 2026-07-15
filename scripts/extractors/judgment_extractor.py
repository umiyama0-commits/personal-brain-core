"""Judgment Extractor — 判断パターン抽出器

入力:
- data/brain/wiki/decisions/*.md      (既存 13 件、明示的な意思決定ログ)
- data/brain/raw/notes/alignment_*.md (本人の自己回答 = 価値観の言語化)
- data/brain/raw/conversations/*.md   (実際の判断の現場)

出力:
- data/brain/wiki/judgment/judgment-<domain>-NNN.md
- data/brain/extractor_state/judgment.json

dedup:
- 既存 wiki/judgment/*.md の (situation, choice_made, underlying_value) を LLM に渡す。

実行:
- python3 /app/scripts/extractors/judgment_extractor.py
- --source decisions|alignment|conversations|all (default: all)
"""
from __future__ import annotations

import argparse
import asyncio
import logging
import sys
from datetime import date
from pathlib import Path

import httpx

sys.path.insert(0, str(Path(__file__).resolve().parent))

from _common import (  # type: ignore  # noqa: E402
    WIKI_DIR,
    ExtractorState,
    LLMContractError,
    call_llm_with_retry,
    existing_pattern_summaries,
    list_alignment_files,
    list_decisions,
    list_raw_conversations,
    list_raw_voice_meetings,
    list_wiki_meetings,
    log_event,
    next_index,
    parse_frontmatter,
    parse_llm_json_array,
    render_frontmatter,
    run_context,
    safe_id,
    short_hash,
)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
)
logger = logging.getLogger("judgment_extractor")

LAYER_DIR = WIKI_DIR / "judgment"
LAYER_DIR.mkdir(parents=True, exist_ok=True)

VALID_DOMAINS = {
    "work",
    "family",
    "tech",
    "finance",
    "ethical",
    "people_management",
    "strategy",
    "brand",
}


PROMPT_TMPL = """あなたは海山丈司 (OWNDAYS CEO) の判断パターン観察者だ。
下記の素材から、彼の **繰り返し現れる判断パターン (judgment pattern)** を抽出する。

判断パターン = 「個別決定」ではなく、「同じような状況で同じように決める傾向」。
1 件の事例 (1 つの decisions ファイル) しか根拠が無いものは、原則 confidence=low。

## 既存判断パターン (重複出力禁止)

下記は既存の判断パターン。意味的に重複するものは絶対に出力しない。
{existing}

## 素材

{materials}

## 出力ルール

- JSON 配列のみ出力。前後の説明・コードフェンス不可。
- 1 件も無ければ `[]`。
- reasoning は「海山自身の言葉」を素材から拾って引用するのが望ましい。
- alternative_rejected は「捨てた選択肢」(無ければ空文字)。
- exceptions は「普段は A だがこういう時は B」という反転条件があれば書く。

```json
[
  {{
    "domain": "work | family | tech | finance | ethical | people_management | strategy | brand",
    "slug": "短い英数 slug (kebab-case)",
    "headline": "短い見出し (10-30 字、日本語可)",
    "situation": "この判断が適用される状況 (1-2 行)",
    "choice_made": "選んだ選択肢",
    "alternative_rejected": "捨てた選択肢 (なければ空)",
    "reasoning": "なぜ — 海山の言葉が拾えれば引用、なければ要約",
    "underlying_value": "根本にある原理 (1 文)",
    "exceptions": "反転条件があれば 1 文 (なければ空)",
    "evidence_hints": ["素材内の根拠箇所を識別する短文 1", "..."],
    "confidence": "low | medium | high",
    "clone_visibility": "public | private",
    "exit_visibility": "public | internal | private"
  }}
]
```

## 判定ルール

- `confidence: high`: 5 件以上の独立した状況で再現、または明示的に言語化された原則。
- `clone_visibility: private`: 重大な人事判断 / 未公開案件 / パートナー関係 / 家族。
  業務全般の判断軸は基本 `public`。
- `exit_visibility`: 人事登用や個人投資判断は `internal`、ブランド判断や働き方は `public`。

それでは抽出開始。"""


def _truncate(s: str, n: int) -> str:
    if len(s) <= n:
        return s
    return s[:n] + "\n...[truncated]"


def _gather_materials(
    state: ExtractorState,
    source: str,
    max_chars_per_file: int = 5000,
    max_total_chars: int = 70000,
) -> tuple[str, list[Path]]:
    candidates: list[Path] = []
    if source in ("decisions", "all"):
        candidates += list_decisions()
    if source in ("alignment", "all"):
        candidates += list_alignment_files()
    if source in ("conversations", "all"):
        candidates += list_raw_conversations()[-20:]
    if source in ("meetings", "all"):
        # ★2026-05-12: 議事録 (wiki/meetings/) + 生 transcript (raw/voice/)
        # 議事録は decisions/action_items が構造化済 = 判断パターン抽出の高密度素材
        # 生 transcript は発言レベルで判断ロジックの観察素材
        candidates += list_wiki_meetings(limit=30)
        candidates += list_raw_voice_meetings(limit=20)

    fresh: list[Path] = []
    for p in candidates:
        try:
            data = p.read_bytes()
        except Exception:
            continue
        h = short_hash(data)
        if state.processed_files.get(str(p)) == h:
            continue
        fresh.append(p)

    if not fresh:
        return "", []

    parts: list[str] = []
    used: list[Path] = []
    total = 0
    for p in fresh:
        try:
            txt = p.read_text(encoding="utf-8", errors="ignore")
        except Exception:
            continue
        snippet = _truncate(txt, max_chars_per_file)
        # parents[2] = data/brain or app/data, depending on cwd; use rglob style
        try:
            rel = p.relative_to(p.parents[2])
        except Exception:
            rel = Path(p.name)
        block = f"\n### source: {rel}\n\n{snippet}\n"
        if total + len(block) > max_total_chars:
            break
        parts.append(block)
        used.append(p)
        total += len(block)
    return "".join(parts), used


def _validate_item(item: dict) -> bool:
    if not isinstance(item, dict):
        return False
    dom = item.get("domain")
    if dom not in VALID_DOMAINS:
        logger.warning(f"invalid domain: {dom}")
        return False
    if not item.get("situation") or not item.get("choice_made"):
        return False
    if not item.get("underlying_value"):
        return False
    conf = item.get("confidence", "low")
    if conf not in ("low", "medium", "high"):
        item["confidence"] = "low"
    return True


def _write_pattern(item: dict, evidence_paths: list[Path]) -> Path:
    dom = item["domain"]
    slug = item.get("slug") or "untitled"
    prefix = f"judgment-{dom}"
    n = next_index(LAYER_DIR, prefix)
    file_id = safe_id(prefix, slug, n)
    out_path = LAYER_DIR / f"{file_id}.md"

    fm = {
        "type": "judgment_pattern",
        "id": file_id,
        "domain": dom,
        "situation": item["situation"].replace("\n", " ").strip(),
        "choice_made": item["choice_made"].replace("\n", " ").strip(),
        "alternative_rejected": (item.get("alternative_rejected") or "").replace("\n", " ").strip(),
        "reasoning": (item.get("reasoning") or "").replace("\n", " ").strip()[:300],
        "underlying_value": item["underlying_value"].replace("\n", " ").strip(),
        "evidence": [
            str(p.relative_to(p.parents[2])) if len(p.parents) >= 3 else p.name
            for p in evidence_paths[:8]
        ],
        "exceptions": [],
        "confidence": item.get("confidence", "low"),
        "last_validated": date.today().isoformat(),
        "clone_visibility": item.get("clone_visibility", "private"),
        "exit_visibility": item.get("exit_visibility", "internal"),
    }

    headline = item.get("headline") or item["choice_made"][:40]
    hints = "\n".join(f"- {h}" for h in item.get("evidence_hints", [])[:6]) or "- (素材中に明確な引用箇所なし)"

    body = f"""# Judgment Pattern: {headline}

## 状況
{item['situation']}

## 選択
- 選んだ: {item['choice_made']}
- 捨てた: {item.get('alternative_rejected') or '(明確な対立選択肢の記録なし)'}

## 理由
{item.get('reasoning') or '(理由の言語化なし)'}

## 根本価値
{item['underlying_value']}

## 例外条項
{item.get('exceptions') or '(現時点では未抽出)'}

## 素材内の根拠
{hints}

## 関連
- [[thinking.md]] (思考傾向の集約)
- [[decisions/_index]] (個別意思決定ログ)
"""
    out = render_frontmatter(fm) + body
    out_path.write_text(out, encoding="utf-8")
    logger.info(f"wrote {out_path.relative_to(WIKI_DIR.parent)} (dom={dom}, conf={fm['confidence']})")
    return out_path


async def run(args: argparse.Namespace) -> None:
    with run_context(
        "judgment_extractor",
        source=args.source,
        model=args.model,
        max_new=args.max_new,
    ) as ctx:
        state = ExtractorState.load("judgment")
        materials, used_files = _gather_materials(
            state,
            source=args.source,
            max_chars_per_file=args.max_chars_per_file,
            max_total_chars=args.max_total_chars,
        )
        ctx["used_files"] = len(used_files)
        if not materials:
            logger.info("no fresh material to process. exiting.")
            ctx["status"] = "no_fresh_raw"
            return

        existing = "\n".join(existing_pattern_summaries(LAYER_DIR, max_files=80)) or "(まだ無し)"
        prompt = PROMPT_TMPL.format(existing=existing, materials=materials)
        logger.info(
            f"calling LLM (model={args.model}, materials={len(materials)} chars, used_files={len(used_files)})"
        )

        async with httpx.AsyncClient() as http:
            try:
                raw_resp = await call_llm_with_retry(
                    http,
                    prompt=prompt,
                    model=args.model,
                    max_tokens=4500,
                    temperature=0.2,
                    timeout=240.0,
                    extractor_name="judgment_extractor",
                )
            except LLMContractError as e:
                logger.error(f"LLM call failed (gave up after retries): {e}")
                ctx["status"] = "llm_failed"
                return

        try:
            items = parse_llm_json_array(
                raw_resp,
                required_keys=("domain", "situation", "choice_made"),
                extractor_name="judgment_extractor",
            )
        except LLMContractError as e:
            logger.error(f"LLM JSON validation failed: {e}\nraw_resp head: {raw_resp[:500]}")
            ctx["status"] = "llm_schema_failed"
            return

        ctx["llm_items_received"] = len(items)
        written = 0
        invalid = 0
        for item in items[: args.max_new]:
            if not _validate_item(item):
                invalid += 1
                continue
            try:
                _write_pattern(item, used_files)
                written += 1
            except Exception as e:
                logger.error(f"failed to write item: {e}")
                log_event(
                    "judgment_extractor",
                    "write_failed",
                    error_msg=str(e)[:200],
                )

        ctx["items_written"] = written
        ctx["items_invalid"] = invalid

        if written > 0 or args.always_mark:
            for p in used_files:
                try:
                    state.processed_files[str(p)] = short_hash(p.read_bytes())
                except Exception:
                    pass
            state.counters["total_written"] = state.counters.get("total_written", 0) + written
            state.save()
            logger.info(f"state saved. wrote {written} new patterns.")
            ctx["status"] = "ok"
        else:
            logger.info("no patterns written, state unchanged.")
            ctx["status"] = "no_patterns"


def main() -> None:
    p = argparse.ArgumentParser(description="Judgment pattern extractor")
    p.add_argument("--source", default="all", choices=["decisions", "alignment", "conversations", "meetings", "all"])
    p.add_argument("--model", default="smart")
    p.add_argument("--max-new", type=int, default=8)
    p.add_argument("--max-chars-per-file", type=int, default=5000)
    p.add_argument("--max-total-chars", type=int, default=70000)
    p.add_argument("--backfill", action="store_true")
    p.add_argument("--always-mark", action="store_true")
    args = p.parse_args()
    asyncio.run(run(args))


if __name__ == "__main__":
    main()
