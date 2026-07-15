"""Reflex Extractor — 反射的反応パターン抽出器

入力:
- data/brain/raw/conversations/*.md   (会話 raw、相手→海山 のペアが取れる)

会話 raw を時系列で読み、(相手の発話) → (海山の即時反応) のペアから
反射候補を抽出する。

出力:
- data/brain/wiki/reflex/reflex-<trigger_slug>-NNN.md
- data/brain/extractor_state/reflex.json

dedup:
- 既存 wiki/reflex/*.md の (trigger, response) を LLM に渡す。

実行:
- python3 /app/scripts/extractors/reflex_extractor.py
- --max-pairs N: 1 回の実行で LLM に渡す会話ペア数 (default 60)
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
    list_raw_conversations,
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
logger = logging.getLogger("reflex_extractor")

LAYER_DIR = WIKI_DIR / "reflex"
LAYER_DIR.mkdir(parents=True, exist_ok=True)


PROMPT_TMPL = """あなたは海山丈司 (OWNDAYS CEO) の **反射的反応** を観察する。

反射 (reflex) = 「考える前に出る」反応。
- 相槌のパターン
- 相手の話への被せ方
- 笑いどころ、ユーモアの瞬間
- 驚き / 困惑 / 怒り の即出反応
- 沈黙への耐性、間の埋め方
- 話題転換のタイミング

判断 (judgment) との違い: judgment は「考えて選ぶ」。reflex は「無意識に出る」。

## 既存反射パターン (重複出力禁止)

下記は既存の反射。意味的に重複するものは絶対に出力しない。
{existing}

## 会話素材

下記は (相手の発話 → 海山の即時反応) を含む会話 raw。
相手の固有名詞は伏せ字 `<相手A>` 等にして evidence に書く。

{materials}

## 出力ルール

- JSON 配列のみ。前後の説明・コードフェンス不可。
- 1 件も明確に観察できなければ `[]`。
- 同じ反射が 1 回しか観察されないなら confidence=low。

```json
[
  {{
    "trigger_slug": "kebab-case slug (例: 'aizuchi-after-emotional-share')",
    "headline": "短い見出し (10-30 字、日本語可)",
    "trigger": "どんな状況/言葉/瞬間が引き金になるか",
    "response": "海山が実際に出す反応 (1-3 行)",
    "modality": "verbal | nonverbal | both",
    "context": "1on1 | group | online | inperson | meeting | casual",
    "examples": [
      "<状況の短い記述> → <海山の反応>",
      "..."
    ],
    "exception": "出ない条件があれば 1 行 (なければ空)",
    "confidence": "low | medium | high",
    "clone_visibility": "public | private",
    "exit_visibility": "public | internal | private"
  }}
]
```

## 判定ルール

- `confidence: high`: 5 例以上 + 複数 context で観察。
- `clone_visibility: private`: 家族/恋愛/医療文脈の反射。
  業務会話・1on1・グループの反射は基本 `public`。
- `exit_visibility`: 業務外で出ると本人イメージを壊しかねないものは `internal`。

それでは抽出開始。"""


def _truncate(s: str, n: int) -> str:
    if len(s) <= n:
        return s
    return s[:n] + "\n...[truncated]"


def _gather_materials(
    state: ExtractorState,
    max_files: int = 30,
    max_chars_per_file: int = 4000,
    max_total_chars: int = 60000,
) -> tuple[str, list[Path]]:
    # ★2026-05-12: 会議 transcript (Plaud / Recall) も追加。
    # 会話 raw + 議事録 transcript 両方読む = 反射的反応の素材が桁違いに増える。
    candidates = list_raw_conversations()[-max_files:] + list_raw_voice_meetings(limit=max_files)
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
        try:
            rel = p.relative_to(p.parents[2])
        except Exception:
            rel = Path(p.name)
        block = f"\n### conv: {rel}\n\n{snippet}\n"
        if total + len(block) > max_total_chars:
            break
        parts.append(block)
        used.append(p)
        total += len(block)
    return "".join(parts), used


def _validate_item(item: dict) -> bool:
    if not isinstance(item, dict):
        return False
    if not item.get("trigger") or not item.get("response"):
        return False
    if item.get("modality") not in ("verbal", "nonverbal", "both"):
        item["modality"] = "verbal"
    if item.get("confidence") not in ("low", "medium", "high"):
        item["confidence"] = "low"
    return True


def _write_pattern(item: dict, evidence_paths: list[Path]) -> Path:
    slug = item.get("trigger_slug") or "untitled"
    prefix = "reflex"
    n = next_index(LAYER_DIR, prefix)
    file_id = safe_id(prefix, slug, n)
    out_path = LAYER_DIR / f"{file_id}.md"

    fm = {
        "type": "reflex_pattern",
        "id": file_id,
        "trigger": item["trigger"].replace("\n", " ").strip(),
        "response": item["response"].replace("\n", " ").strip(),
        "modality": item["modality"],
        "context": item.get("context", "1on1"),
        "evidence": [
            str(p.relative_to(p.parents[2])) if len(p.parents) >= 3 else p.name
            for p in evidence_paths[:8]
        ],
        "counter_evidence": [],
        "confidence": item.get("confidence", "low"),
        "last_observed": date.today().isoformat(),
        "clone_visibility": item.get("clone_visibility", "private"),
        "exit_visibility": item.get("exit_visibility", "internal"),
    }

    headline = item.get("headline") or item["response"][:40]
    examples_md = "\n".join(f"- {ex}" for ex in item.get("examples", [])[:5]) or "- (例なし)"
    exception = item.get("exception") or ""

    body = f"""# Reflex Pattern: {headline}

## トリガー
{item['trigger']}

## 反応
{item['response']}

## モダリティ
{item['modality']}

## 例
{examples_md}

## 例外
{exception or '(現時点では未観察)'}

## 関連
- [[style/_index]]
- [[embodiment/_index]]
"""
    out = render_frontmatter(fm) + body
    out_path.write_text(out, encoding="utf-8")
    logger.info(f"wrote {out_path.relative_to(WIKI_DIR.parent)} (modality={fm['modality']}, conf={fm['confidence']})")
    return out_path


async def run(args: argparse.Namespace) -> None:
    with run_context(
        "reflex_extractor",
        model=args.model,
        max_new=args.max_new,
    ) as ctx:
        state = ExtractorState.load("reflex")
        materials, used_files = _gather_materials(
            state,
            max_files=args.max_files,
            max_chars_per_file=args.max_chars_per_file,
            max_total_chars=args.max_total_chars,
        )
        ctx["used_files"] = len(used_files)
        if not materials:
            logger.info("no fresh conversations. exiting.")
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
                    max_tokens=4000,
                    temperature=0.3,
                    timeout=240.0,
                    extractor_name="reflex_extractor",
                )
            except LLMContractError as e:
                logger.error(f"LLM call failed (gave up after retries): {e}")
                ctx["status"] = "llm_failed"
                return

        try:
            items = parse_llm_json_array(
                raw_resp,
                required_keys=("trigger", "response", "modality"),
                extractor_name="reflex_extractor",
            )
        except LLMContractError as e:
            logger.error(f"LLM JSON validation failed: {e}\nhead: {raw_resp[:500]}")
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
                    "reflex_extractor",
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
            logger.info(f"state saved. wrote {written} new reflexes.")
            ctx["status"] = "ok"
        else:
            logger.info("no patterns written, state unchanged.")
            ctx["status"] = "no_patterns"


def main() -> None:
    p = argparse.ArgumentParser(description="Reflex pattern extractor")
    p.add_argument("--model", default="smart")
    p.add_argument("--max-new", type=int, default=8)
    p.add_argument("--max-files", type=int, default=30)
    p.add_argument("--max-chars-per-file", type=int, default=4000)
    p.add_argument("--max-total-chars", type=int, default=60000)
    p.add_argument("--backfill", action="store_true")
    p.add_argument("--always-mark", action="store_true")
    args = p.parse_args()
    asyncio.run(run(args))


if __name__ == "__main__":
    main()
