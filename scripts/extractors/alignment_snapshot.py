"""Alignment Snapshot — 月次「本人像スナップショット」自動生成

Self-replication foundation Step 8。
`meta/alignment_state.md` に月初 1 日に新しいブロックを追記する。

設計思想:
- 集約 wiki (identity.md / style.md / thinking.md) と、
  個別パターン (wiki/style/* / wiki/judgment/* / wiki/reflex/* / wiki/embodiment/*) の
  直近 days 日の差分を LLM に渡して「この月で何が変わったか」を要約。
- メトリクス (raw / wiki / drift / audit のカウント) は LLM ではなく Python で直接集計
  (LLM に数字を持たせると hallucinate するため決定論的に処理)。
- 同じ月のスナップショットが既にある場合はスキップ (--force で上書き)。
- LLM が壊れたら LLMContractError → run_context が run_failed をログ → 上流が拾える。

実行:
- python3 scripts/extractors/alignment_snapshot.py             # dry run (生成 markdown を stdout)
- python3 scripts/extractors/alignment_snapshot.py --apply     # alignment_state.md に挿入
- python3 scripts/extractors/alignment_snapshot.py --month 2026-05 --apply
- python3 scripts/extractors/alignment_snapshot.py --apply --force  # 既存月を上書き

events.jsonl:
- run_started / run_finished (status: ok|already_exists|llm_failed|llm_schema_failed)
- llm_call_failed (1 attempt ごと)
"""
from __future__ import annotations

import argparse
import asyncio
import json
import logging
import re
import sys
from dataclasses import asdict, dataclass
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any

import httpx

sys.path.insert(0, str(Path(__file__).resolve().parent))

from _common import (  # type: ignore  # noqa: E402
    AUDIT_DIR,
    LLMContractError,
    META_DIR,
    RAW_DIR,
    WIKI_DIR,
    call_llm_with_retry,
    extract_json_block,
    log_event,
    parse_frontmatter,
    render_frontmatter,
    run_context,
)

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger("alignment_snapshot")

ALIGNMENT_STATE = META_DIR / "alignment_state.md"
DRIFT_LOG = META_DIR / "drift_log.md"
PENDING_QUESTIONS = AUDIT_DIR / "pending_questions.md"
RESOLVED_DIR = AUDIT_DIR / "resolved"

EXTRACTOR_NAME = "alignment_snapshot"

REQUIRED_KEYS = (
    "interests",
    "judgment_trends",
    "language_changes",
    "reflex_changes",
    "embodiment_notes",
    "free_text",
)


# ─── メトリクス (決定論的集計) ─────────────────────────
@dataclass
class AlignmentMetrics:
    raw_conversations: int
    raw_notes: int
    wiki_style: int
    wiki_judgment: int
    wiki_reflex: int
    wiki_embodiment: int
    wiki_decisions: int
    wiki_knowledge: int
    wiki_people: int
    wiki_projects: int
    drift_entries_recent: int  # 過去 days 日の drift_log エントリ数
    audit_pending: int
    audit_resolved: int


def _count_md(d: Path) -> int:
    if not d.exists():
        return 0
    return sum(1 for f in d.glob("*.md") if not f.name.startswith("_"))


def _count_drift_recent(days: int) -> int:
    """drift_log.md の中で直近 days 日以内のエントリ数を概算。

    drift_log は append-only の自由形式 markdown を想定。
    `## YYYY-MM-DD` ヘッダ or `[YYYY-MM-DD]` でエントリ単位を判定する。
    """
    if not DRIFT_LOG.exists():
        return 0
    text = DRIFT_LOG.read_text(encoding="utf-8")
    cutoff = date.today() - timedelta(days=days)
    n = 0
    # 日付らしき表記を全部拾う (## YYYY-MM-DD or [YYYY-MM-DD] or ### YYYY-MM-DD)
    for m in re.finditer(r"(\d{4})-(\d{2})-(\d{2})", text):
        try:
            d = date(int(m.group(1)), int(m.group(2)), int(m.group(3)))
        except ValueError:
            continue
        if d >= cutoff:
            n += 1
    return n


def _count_audit() -> tuple[int, int]:
    """audit/pending_questions.md の pending 数 + audit/resolved/ の解決済み数"""
    pending = 0
    if PENDING_QUESTIONS.exists():
        text = PENDING_QUESTIONS.read_text(encoding="utf-8")
        # 各 Q ブロックの "状態: pending" をカウント (= 解決前)
        pending = len(re.findall(r"\*\*状態\*\*:\s*pending\b", text))
    resolved = 0
    if RESOLVED_DIR.exists():
        resolved = sum(1 for f in RESOLVED_DIR.glob("*.md") if not f.name.startswith("_"))
    return pending, resolved


def gather_metrics(days: int = 35) -> AlignmentMetrics:
    return AlignmentMetrics(
        raw_conversations=_count_md(RAW_DIR / "conversations"),
        raw_notes=_count_md(RAW_DIR / "notes"),
        wiki_style=_count_md(WIKI_DIR / "style"),
        wiki_judgment=_count_md(WIKI_DIR / "judgment"),
        wiki_reflex=_count_md(WIKI_DIR / "reflex"),
        wiki_embodiment=_count_md(WIKI_DIR / "embodiment"),
        wiki_decisions=_count_md(WIKI_DIR / "decisions"),
        wiki_knowledge=_count_md(WIKI_DIR / "knowledge"),
        wiki_people=_count_md(WIKI_DIR / "people"),
        wiki_projects=_count_md(WIKI_DIR / "projects"),
        drift_entries_recent=_count_drift_recent(days),
        audit_pending=_count_audit()[0],
        audit_resolved=_count_audit()[1],
    )


# ─── 直近差分の収集 ────────────────────────────────
def gather_recent_patterns(days: int = 35) -> dict[str, list[str]]:
    """各層 (style/judgment/reflex/embodiment) で直近 days 日に
    追加 or 更新されたファイルから "id: pattern" を抜く。

    LLM に渡して「この月で何が観測されたか」を要約してもらう。
    多すぎると context を圧迫するため層あたり 30 件まで。
    """
    cutoff = (datetime.now() - timedelta(days=days)).timestamp()
    out: dict[str, list[str]] = {}
    for layer in ("style", "judgment", "reflex", "embodiment"):
        d = WIKI_DIR / layer
        if not d.exists():
            out[layer] = []
            continue
        recent = []
        for f in sorted(d.glob("*.md")):
            if f.name.startswith("_"):
                continue
            try:
                mtime = f.stat().st_mtime
            except OSError:
                continue
            if mtime < cutoff:
                continue
            try:
                content = f.read_text(encoding="utf-8")
            except OSError:
                continue
            fm, _ = parse_frontmatter(content)
            pid = fm.get("id", f.stem)
            descr = (
                fm.get("pattern")
                or fm.get("trigger")
                or fm.get("situation")
                or fm.get("modality")
                or ""
            )
            recent.append(f"- {pid}: {descr}".strip())
            if len(recent) >= 30:
                break
        out[layer] = recent
    return out


def gather_aggregated_sources() -> dict[str, str]:
    """集約 wiki (identity / style / thinking) を読み込む。
    LLM に投入する前に長すぎる場合は切り詰める。
    """
    out = {}
    for fname in ("identity.md", "style.md", "thinking.md"):
        p = WIKI_DIR / fname
        if not p.exists():
            out[fname] = ""
            continue
        try:
            content = p.read_text(encoding="utf-8")
        except OSError:
            content = ""
        # frontmatter 落とす + 8K 上限
        _, body = parse_frontmatter(content)
        out[fname] = body.strip()[:8000]
    return out


# ─── alignment_state.md の解析・操作 ────────────────────
SNAPSHOT_HEADER_RE = re.compile(r"^### (\d{4}-\d{2})\b", re.MULTILINE)
SNAPSHOT_SECTION_RE = re.compile(
    r"^## スナップショット\b.*?$",
    re.MULTILINE,
)


def existing_months(state_text: str) -> set[str]:
    """alignment_state.md に既にある月 (YYYY-MM) を全部抜く。"""
    return set(SNAPSHOT_HEADER_RE.findall(state_text))


def remove_month(state_text: str, month: str) -> str:
    """既存の YYYY-MM ブロックを削除 (--force 用)。

    `### YYYY-MM` から次の `### YYYY-MM` (or EOF) までを削除。
    """
    pattern = re.compile(
        rf"^### {re.escape(month)}\b.*?(?=^### \d{{4}}-\d{{2}}\b|\Z)",
        re.MULTILINE | re.DOTALL,
    )
    return pattern.sub("", state_text)


def render_snapshot_block(
    month: str,
    content: dict,
    metrics: AlignmentMetrics,
    note: str = "",
) -> str:
    """LLM の構造化 dict + 決定論的メトリクスから markdown ブロックを生成。"""

    def _bullets(items: Any, fallback: str = "(該当なし)") -> str:
        if isinstance(items, list) and items:
            return "\n".join(f"- {str(it).strip()}" for it in items if str(it).strip())
        if isinstance(items, str) and items.strip():
            return f"- {items.strip()}"
        return f"- {fallback}"

    free = content.get("free_text", "")
    if isinstance(free, list):
        free = "\n".join(str(x).strip() for x in free if str(x).strip())
    free = free.strip() or "(該当なし)"

    suffix = f" ({note})" if note else ""

    block = f"""### {month}{suffix}

#### 直近の関心 ({month})
{_bullets(content.get("interests"))}

#### 判断軸の傾向 ({month})
{_bullets(content.get("judgment_trends"))}

#### 言葉の癖の変化 ({month})
{_bullets(content.get("language_changes"))}

#### 反射の変化 ({month})
{_bullets(content.get("reflex_changes"))}

#### 身体性メモ ({month})
{_bullets(content.get("embodiment_notes"))}

#### 自由記述 ({month})
{free}

#### 主要指標 ({month})
- raw/conversations: **{metrics.raw_conversations} 件**
- raw/notes: **{metrics.raw_notes} 件**
- wiki/style: **{metrics.wiki_style} 件** / wiki/judgment: **{metrics.wiki_judgment} 件** / wiki/reflex: **{metrics.wiki_reflex} 件** / wiki/embodiment: **{metrics.wiki_embodiment} 件**
- wiki/decisions: **{metrics.wiki_decisions} 件** / wiki/knowledge: **{metrics.wiki_knowledge} 件** / wiki/people: **{metrics.wiki_people} 件** / wiki/projects: **{metrics.wiki_projects} 件**
- drift_log エントリ (直近): **{metrics.drift_entries_recent} 件**
- audit pending: **{metrics.audit_pending} 件** / resolved: **{metrics.audit_resolved} 件**
"""
    return block


def insert_snapshot(state_text: str, block: str) -> str:
    """alignment_state.md の "## スナップショット (時系列、新しいものを上に追記)"
    の直後に新しいスナップショットブロックを挿入。

    section が無ければ末尾に追加 + section ヘッダ自体も追加。
    """
    target = "## スナップショット (時系列、新しいものを上に追記)"
    if target in state_text:
        # section header の次の空行までスキップ → そこに挿入
        idx = state_text.index(target)
        # ヘッダ行末まで進む
        line_end = state_text.find("\n", idx)
        if line_end < 0:
            return state_text + "\n\n" + block
        # ヘッダ直後 (改行を含めて 1 個 or 2 個進む) に挿入
        before = state_text[: line_end + 1]
        after = state_text[line_end + 1 :]
        # 後続が空行で始まるなら 1 行スキップ
        if after.startswith("\n"):
            return before + "\n" + block.rstrip() + "\n" + after
        return before + "\n" + block.rstrip() + "\n\n" + after
    # section がない → 末尾に追加
    return (
        state_text.rstrip()
        + "\n\n## スナップショット (時系列、新しいものを上に追記)\n\n"
        + block.rstrip()
        + "\n"
    )


def update_state_frontmatter(state_text: str, today: str) -> str:
    """frontmatter の updated を today に、snapshot_index を +1 する。"""
    fm, body = parse_frontmatter(state_text)
    if not fm:
        return state_text
    fm["updated"] = today
    try:
        idx = int(fm.get("snapshot_index", 0))
    except (TypeError, ValueError):
        idx = 0
    fm["snapshot_index"] = idx + 1
    return render_frontmatter(fm) + body


# ─── LLM 呼び出し ──────────────────────────────────
def build_prompt(
    month: str,
    aggregated: dict[str, str],
    recent_patterns: dict[str, list[str]],
    metrics: AlignmentMetrics,
    prior_snapshot_excerpt: str = "",
) -> str:
    layer_blocks = []
    for layer in ("style", "judgment", "reflex", "embodiment"):
        items = recent_patterns.get(layer, [])
        block = (
            f"### wiki/{layer}/ 直近追加・更新 ({len(items)} 件)\n"
            + ("\n".join(items) if items else "(該当なし)")
        )
        layer_blocks.append(block)

    prior_section = (
        f"\n## 前回スナップショット (差分検出の参照点)\n{prior_snapshot_excerpt[:4000]}\n"
        if prior_snapshot_excerpt
        else ""
    )

    metrics_block = json.dumps(asdict(metrics), ensure_ascii=False, indent=2)

    return f"""あなたは「海山丈司の Personal Brain」の月次スナップショット担当。
今月 ({month}) の「本人像スナップショット」を作る。

# 目的
- `meta/alignment_state.md` に追記する 1 ブロックの**構造化 JSON** を返す。
- 「2026-04 の海山像が永続化されてしまう」ことを防ぐため、**この月で何が変わったか**を中心に書く。
- 不明・該当なしの項目は空配列でよい (無理に埋めない)。

# 入力 (集約 wiki - 腐りにくい本質)
## identity.md (抜粋)
{aggregated.get("identity.md", "")[:3000]}

## style.md (抜粋)
{aggregated.get("style.md", "")[:3000]}

## thinking.md (抜粋)
{aggregated.get("thinking.md", "")[:3000]}

# 入力 (個別パターン - 直近観測された変化)
{chr(10).join(layer_blocks)}

# 入力 (主要指標 - 決定論的集計、参考値)
{metrics_block}
{prior_section}
# 出力ルール
- 必ず**単一の JSON オブジェクト**を返す (配列ではない、説明文も付けない)。
- ```json フェンスは付けても付けなくてもよい。
- 各キーの値は文字列の配列 (free_text のみ markdown 段落 1 つの文字列)。
- 配列の各要素は短く (1-2 文)。固有名詞は具体的に。
- 数字を出す時は metrics の値と整合させる (改めて推測しない)。
- 日本語で。

# 出力スキーマ
```json
{{
  "interests": ["この月、海山の関心が向いた話題・問い (3-7 件)"],
  "judgment_trends": ["この月の意思決定で繰り返し出た優先順位・ポリシー (3-7 件)"],
  "language_changes": ["増えた語彙・減った語彙・新しい言い回し。観測できなければ空配列"],
  "reflex_changes": ["相手対応・場の取り回しで変化があれば。空配列可"],
  "embodiment_notes": ["疲労感・声の張り・健康に関する観測。embodiment 層が空なら空配列"],
  "free_text": "数値化しきれない感覚的な変化。1 段落。markdown 可。"
}}
```

JSON のみを返せ。説明文・前置きは付けない。
"""


async def request_snapshot(
    month: str,
    aggregated: dict[str, str],
    recent_patterns: dict[str, list[str]],
    metrics: AlignmentMetrics,
    prior_snapshot_excerpt: str = "",
    model: str = "smart",
    timeout: float = 180.0,
) -> dict:
    prompt = build_prompt(month, aggregated, recent_patterns, metrics, prior_snapshot_excerpt)
    async with httpx.AsyncClient() as http:
        text = await call_llm_with_retry(
            http,
            prompt,
            model=model,
            max_tokens=2500,
            temperature=0.3,
            timeout=timeout,
            extractor_name=EXTRACTOR_NAME,
        )
    try:
        data = extract_json_block(text)
    except (json.JSONDecodeError, ValueError) as e:
        log_event(EXTRACTOR_NAME, "llm_parse_failed", error_msg=str(e)[:200], text_preview=text[:300])
        raise LLMContractError(f"snapshot LLM did not return JSON: {e}") from e

    if not isinstance(data, dict):
        log_event(EXTRACTOR_NAME, "llm_schema_failed", reason="not_a_dict", type=type(data).__name__)
        raise LLMContractError(f"snapshot LLM returned {type(data).__name__}, expected dict")

    missing = [k for k in REQUIRED_KEYS if k not in data]
    if missing:
        log_event(EXTRACTOR_NAME, "llm_schema_failed", reason="missing_keys", missing=missing)
        raise LLMContractError(f"snapshot LLM JSON missing keys: {missing}")
    return data


# ─── 前回スナップショット抜粋 ───────────────────────────
def extract_prior_snapshot(state_text: str, current_month: str) -> str:
    """current_month を除いて、最も新しい既存スナップショットの本文を返す。
    LLM に「前回からの差分」を意識させるため。
    """
    months = existing_months(state_text)
    months.discard(current_month)
    if not months:
        return ""
    latest = sorted(months)[-1]
    # ### latest ... から次の ### YYYY-MM (or EOF) までを抜く
    pattern = re.compile(
        rf"^### {re.escape(latest)}\b.*?(?=^### \d{{4}}-\d{{2}}\b|\Z)",
        re.MULTILINE | re.DOTALL,
    )
    m = pattern.search(state_text)
    return m.group(0) if m else ""


# ─── メイン処理 ───────────────────────────────────
async def run(month: str, apply: bool, force: bool, model: str, days: int, note: str) -> dict:
    """1 回分の snapshot 生成・挿入を実行。

    返り値: ctx 用の dict (status / month / inserted など)。
    """
    if not ALIGNMENT_STATE.exists():
        # ファイル自体無ければ最低限の枠を作る
        ALIGNMENT_STATE.parent.mkdir(parents=True, exist_ok=True)
        ALIGNMENT_STATE.write_text(
            "---\ntype: alignment_snapshot\nupdated: "
            + date.today().isoformat()
            + "\nsnapshot_index: 0\nclone_visibility: private\nexit_visibility: private\n---\n"
            "# Alignment State — 本人像スナップショット\n\n"
            "## スナップショット (時系列、新しいものを上に追記)\n\n",
            encoding="utf-8",
        )

    state_text = ALIGNMENT_STATE.read_text(encoding="utf-8")
    months = existing_months(state_text)

    if month in months and not force:
        logger.info(f"snapshot for {month} already exists. skip (use --force to overwrite)")
        return {"status": "already_exists", "month": month}

    metrics = gather_metrics(days=days)
    aggregated = gather_aggregated_sources()
    recent_patterns = gather_recent_patterns(days=days)
    prior = extract_prior_snapshot(state_text, month)

    logger.info(
        f"generating snapshot {month} (days={days}, prior={'yes' if prior else 'no'}, model={model})"
    )

    content = await request_snapshot(
        month=month,
        aggregated=aggregated,
        recent_patterns=recent_patterns,
        metrics=metrics,
        prior_snapshot_excerpt=prior,
        model=model,
    )

    block = render_snapshot_block(month, content, metrics, note=note)

    if not apply:
        logger.info("(dry run — re-run with --apply to insert)")
        print(block)
        return {"status": "dry_run", "month": month, "block_chars": len(block)}

    # apply: 挿入
    if month in months and force:
        state_text = remove_month(state_text, month)
        logger.info(f"removed existing {month} block (force=True)")

    new_text = insert_snapshot(state_text, block)
    new_text = update_state_frontmatter(new_text, date.today().isoformat())
    ALIGNMENT_STATE.write_text(new_text, encoding="utf-8")
    logger.info(f"inserted snapshot {month} into {ALIGNMENT_STATE}")
    return {
        "status": "applied",
        "month": month,
        "block_chars": len(block),
        "metrics": asdict(metrics),
    }


def main() -> None:
    ap = argparse.ArgumentParser(description="Monthly alignment snapshot generator")
    ap.add_argument("--month", default=None, help="YYYY-MM (default: today)")
    ap.add_argument("--apply", action="store_true", help="alignment_state.md に書き込む")
    ap.add_argument("--force", action="store_true", help="既存月を上書き")
    ap.add_argument("--model", default="smart", help="LLM model (default: smart)")
    ap.add_argument("--days", type=int, default=35, help="直近何日を window にするか (default: 35)")
    ap.add_argument("--note", default="", help="月ヘッダの (...) 内に入れる注記")
    args = ap.parse_args()

    month = args.month or date.today().strftime("%Y-%m")
    if not re.match(r"^\d{4}-\d{2}$", month):
        raise SystemExit(f"invalid --month: {month}. use YYYY-MM")

    with run_context(
        EXTRACTOR_NAME,
        month=month,
        apply=args.apply,
        force=args.force,
        days=args.days,
        model=args.model,
    ) as ctx:
        try:
            result = asyncio.run(
                run(
                    month=month,
                    apply=args.apply,
                    force=args.force,
                    model=args.model,
                    days=args.days,
                    note=args.note,
                )
            )
        except LLMContractError as e:
            ctx["status"] = "llm_failed"
            ctx["error"] = str(e)[:200]
            logger.error(f"LLM contract failure: {e}")
            return
        # start_fields と被る key (month/apply/force/days/model) は ctx に入れない
        # (run_context が **start_fields, **ctx を log_event に渡す時に衝突するため)
        skip_keys = {"month", "apply", "force", "days", "model", "metrics"}
        ctx.update({k: v for k, v in result.items() if k not in skip_keys})
        if "metrics" in result:
            # メトリクスは run_finished に展開して入れる (集計しやすい)
            for k, v in result["metrics"].items():
                ctx[f"metric_{k}"] = v


if __name__ == "__main__":
    main()
