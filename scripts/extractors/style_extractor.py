"""Style Extractor — 言語パターン抽出器

raw (alignment 回答 + recent conversations + recent notes) を読み、
海山の文体パターンを LLM 経由で個別ファイル化する。

入力:
- data/brain/raw/notes/alignment_*.md (Privacy Gate 通過済み、最も整った素材)
- data/brain/raw/conversations/*.md   (Privacy Gate 通過済み、口語の宝庫)
- data/brain/raw/notes/*.md           (上記以外の note)

出力:
- data/brain/wiki/style/style-<category>-NNN.md (frontmatter + 本文)
- data/brain/extractor_state/style.json (sha256 で processed file を追跡)

dedup:
- 既存 wiki/style/*.md の pattern サマリを LLM に渡し、
  「これらと意味的に重複するものは出すな」と指示する。

実行:
- 一回限り (backfill):  python3 /app/scripts/extractors/style_extractor.py --backfill
- 差分 (default):       python3 /app/scripts/extractors/style_extractor.py
- 件数制限:             --max-new 5 (LLM が出した中から最初の N 件だけ採用)
- 対象種類:             --source alignment|conversations|notes|all (default: all)
"""
from __future__ import annotations

import argparse
import asyncio
import json
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
    list_raw_conversations,
    list_raw_notes,
    list_raw_voice_meetings,
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
logger = logging.getLogger("style_extractor")

LAYER_DIR = WIKI_DIR / "style"
LAYER_DIR.mkdir(parents=True, exist_ok=True)

VALID_CATEGORIES = {
    "vocabulary",
    "sentence_ending",
    "metaphor",
    "code_switching",
    "rhythm",
    "punctuation",
    "greeting",
    "closing",
}
VALID_CONTEXTS = {
    "casual_chat",
    "formal_doc",
    "meeting",
    "family",
    "line_works",
    "line_personal",
    "one_on_one",
    "group",
}


# ─── Prompt ───────────────────────────────────────────────────────
PROMPT_TMPL = """あなたは海山丈司 (OWNDAYS CEO) の文体観察者だ。
下記の raw 素材から、彼の **言語パターン (style pattern)** を抽出する。

## 既存パターン (重複出力禁止)

下記は既存の style パターン。意味的に重複するものは絶対に出力しない。
{existing}

## raw 素材

{materials}

## 出力ルール

- JSON 配列のみ出力。前後の説明・コードフェンス不可。
- 各要素は下記スキーマ。
- 1 件も該当パターンがなければ `[]` を返す。
- 過剰に細分化しない。本当に「文体の癖」と言えるレベルだけ。
- evidence は raw 素材内の **元発話 (引用)** を 2 件以上含める (短くてよい、原文ママ)。

```json
[
  {{
    "category": "vocabulary | sentence_ending | metaphor | code_switching | rhythm | punctuation | greeting | closing",
    "context": "casual_chat | formal_doc | meeting | family | line_works | line_personal | one_on_one | group",
    "slug": "短い英数 slug (kebab-case, 例: 'short-decisive-sentences')",
    "headline": "短い見出し (10-30 字、日本語可)",
    "pattern": "観察可能なパターンを 1-3 行で具体に",
    "examples": ["原文ママの引用 1", "原文ママの引用 2", "..."],
    "exception": "崩れる条件があれば 1 行 (なければ空文字)",
    "confidence": "low | medium | high",
    "context_observed_in": ["casual_chat", "formal_doc"],
    "clone_visibility": "public | private",
    "exit_visibility": "public | internal | private"
  }}
]
```

## 判定基準

- `confidence: high` を付けるのは「複数 context で 5 件以上の例」かつ
  「明確に他者と区別できる癖」に限る。
- `clone_visibility: private` にするのは、家族/性的/悪口/医療/パートナー文脈。
  業務全般の口語癖は `public`。
- `exit_visibility`: 業務外で踏まれると本人イメージを壊しかねないものは `internal`、
  完全公開可能なものは `public`、極めてプライベートなものは `private`。

それでは抽出開始。"""


def _truncate(s: str, n: int) -> str:
    if len(s) <= n:
        return s
    return s[:n] + "\n...[truncated]"


def _gather_materials(
    state: ExtractorState,
    source: str,
    max_chars_per_file: int = 6000,
    max_total_chars: int = 60000,
) -> tuple[str, list[Path]]:
    """sources を集めて (LLM 入力用 string, processed 候補 list) を返す。"""
    candidates: list[Path] = []
    if source in ("alignment", "all"):
        candidates += list_alignment_files()
    if source in ("conversations", "all"):
        # 直近のみ (raw conv は数千ある可能性、最新 30 だけ)
        candidates += list_raw_conversations()[-30:]
    if source in ("notes", "all"):
        # alignment 以外の最近 note
        all_notes = list_raw_notes()
        non_align = [p for p in all_notes if not p.name.startswith("alignment_")]
        candidates += non_align[-30:]
    if source in ("meetings", "all"):
        # ★2026-05-12: 会議 transcript (Plaud / Recall / Owl) は海山の発言の宝庫
        # 話者識別付きで実発言が並ぶ → 言語パターン抽出の主力素材
        candidates += list_raw_voice_meetings(limit=30)

    # 既処理 skip
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
        block = f"\n### raw_id: {p.relative_to(p.parents[2])}\n\n{snippet}\n"
        if total + len(block) > max_total_chars:
            break
        parts.append(block)
        used.append(p)
        total += len(block)

    return "".join(parts), used


def _validate_item(item: dict) -> bool:
    if not isinstance(item, dict):
        return False
    cat = item.get("category")
    if cat not in VALID_CATEGORIES:
        logger.warning(f"invalid category: {cat}")
        return False
    if not item.get("pattern"):
        return False
    examples = item.get("examples") or []
    if not isinstance(examples, list) or len(examples) < 2:
        logger.warning(f"need >=2 examples (got {len(examples) if isinstance(examples, list) else 'not-list'})")
        return False
    conf = item.get("confidence", "low")
    if conf not in ("low", "medium", "high"):
        item["confidence"] = "low"
    return True


def _write_pattern(item: dict, evidence_paths: list[Path]) -> Path | None:
    cat = item["category"]
    slug = item.get("slug") or "untitled"
    prefix = f"style-{cat}"
    n = next_index(LAYER_DIR, prefix)
    file_id = safe_id(prefix, slug, n)
    out_path = LAYER_DIR / f"{file_id}.md"

    fm = {
        "type": "style_pattern",
        "id": file_id,
        "category": cat,
        "context": item.get("context", "casual_chat"),
        "pattern": item["pattern"].replace("\n", " ").strip(),
        "evidence": [str(p.relative_to(p.parents[2])) for p in evidence_paths[:8]],
        "counter_evidence": [],
        "confidence": item.get("confidence", "low"),
        "last_updated": date.today().isoformat(),
        "clone_visibility": item.get("clone_visibility", "private"),
        "exit_visibility": item.get("exit_visibility", "internal"),
    }

    headline = item.get("headline") or item["pattern"][:40]
    examples_md = "\n".join(f"- \"{ex}\"" for ex in item.get("examples", [])[:6])
    exception = item.get("exception") or ""
    contexts = item.get("context_observed_in") or [fm["context"]]

    body = f"""# Style Pattern: {headline}

## 観察
{item['pattern']}

観察された context: {', '.join(contexts)}

## 例
{examples_md}

## 例外 / 反証
{exception or '(現時点では未観察)'}

## 関連
- [[style.md]] (集約サマリ)
"""

    out = render_frontmatter(fm) + body
    out_path.write_text(out, encoding="utf-8")
    logger.info(f"wrote {out_path.relative_to(WIKI_DIR.parent)} (cat={cat}, conf={fm['confidence']})")
    return out_path


async def run(args: argparse.Namespace) -> None:
    with run_context(
        "style_extractor",
        source=args.source,
        model=args.model,
        max_new=args.max_new,
    ) as ctx:
        state = ExtractorState.load("style")
        materials, used_files = _gather_materials(
            state,
            source=args.source,
            max_chars_per_file=args.max_chars_per_file,
            max_total_chars=args.max_total_chars,
        )
        ctx["used_files"] = len(used_files)

        if not materials:
            logger.info("no fresh raw to process. exiting.")
            ctx["status"] = "no_fresh_raw"
            return

        existing = "\n".join(existing_pattern_summaries(LAYER_DIR, max_files=80)) or "(まだ無し)"

        prompt = PROMPT_TMPL.format(existing=existing, materials=materials)
        logger.info(
            f"calling LLM (model={args.model}, materials={len(materials)} chars, "
            f"used_files={len(used_files)}, existing={len(existing.splitlines())} patterns)"
        )

        async with httpx.AsyncClient() as http:
            try:
                raw_resp = await call_llm_with_retry(
                    http,
                    prompt=prompt,
                    model=args.model,
                    max_tokens=4000,
                    temperature=0.2,
                    timeout=180.0,
                    extractor_name="style_extractor",
                )
            except LLMContractError as e:
                logger.error(f"LLM call failed (gave up after retries): {e}")
                ctx["status"] = "llm_failed"
                return

        try:
            items = parse_llm_json_array(
                raw_resp,
                required_keys=("category", "context", "pattern"),
                extractor_name="style_extractor",
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
                    "style_extractor",
                    "write_failed",
                    error_msg=str(e)[:200],
                )

        ctx["items_written"] = written
        ctx["items_invalid"] = invalid

        # state 更新 (LLM が成功した時のみ processed としてマーク)
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
            logger.info("no patterns written, state unchanged (will retry next run).")
            ctx["status"] = "no_patterns"


def main() -> None:
    p = argparse.ArgumentParser(description="Style pattern extractor")
    p.add_argument("--source", default="all", choices=["alignment", "conversations", "notes", "meetings", "all"])
    p.add_argument("--model", default="smart", help="LiteLLM model alias (default: smart)")
    p.add_argument("--max-new", type=int, default=10, help="LLM 出力から採用する上限")
    p.add_argument("--max-chars-per-file", type=int, default=6000)
    p.add_argument("--max-total-chars", type=int, default=60000)
    p.add_argument("--backfill", action="store_true", help="(現在は default の差分実行と同義)")
    p.add_argument("--always-mark", action="store_true", help="0 件でも processed としてマーク")
    args = p.parse_args()
    asyncio.run(run(args))


if __name__ == "__main__":
    main()
