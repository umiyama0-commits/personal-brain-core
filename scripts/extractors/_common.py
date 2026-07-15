"""抽出器共通ユーティリティ。

全 extractor で共有する:
- LiteLLM 呼び出し (リトライ + JSON 検証付き)
- raw / wiki ファイル発見
- ID 生成 (style-001 等)
- frontmatter 解析・書き込み
- 進行状態の永続化 (state json)
- 構造化ログ (events.jsonl) と run コンテキスト
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import random
import re
import time
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import date, datetime
from pathlib import Path
from typing import Any, Awaitable, Callable, Iterable, Optional

import httpx

# パス定数 (docker container 内 /app から見たもの)
APP_ROOT = Path(os.getenv("BRAIN_APP_ROOT", "/app"))
DATA_BRAIN = APP_ROOT / "data" / "brain"
RAW_DIR = DATA_BRAIN / "raw"
WIKI_DIR = DATA_BRAIN / "wiki"
META_DIR = DATA_BRAIN / "meta"
AUDIT_DIR = DATA_BRAIN / "audit"
SCHEMA_DIR = DATA_BRAIN / "schema"
STATE_DIR = DATA_BRAIN / "extractor_state"  # 各 extractor の進行状態
# NOTE: import 時の mkdir は副作用として有害 (test 環境 / host 環境で
# /app が無いと ImportError になる)。実際に書き込む時点で _ensure_log_dir /
# ExtractorState.save 内で作成する。

LITELLM_URL = os.getenv("LITELLM_URL", "http://litellm:4000")
LITELLM_KEY = os.getenv("LITELLM_MASTER_KEY", "")

JST_TODAY = date.today().isoformat()

# 構造化ログ (events.jsonl) の出力先
EVENTS_LOG = STATE_DIR / "events.jsonl"

# LiteLLM 呼び出しのデフォルト
DEFAULT_LLM_RETRIES = 3
DEFAULT_LLM_BACKOFF_BASE = 1.5  # 秒


class LLMContractError(RuntimeError):
    """LLM 呼び出し or JSON 検証の失敗を表す例外。

    リトライを尽くしても回復できなかった時に raise する。
    extractor 側で捕捉して "LLM が壊れた" のか "本当に 0 件" なのかを
    切り分けるために使う (silent skip の根本原因)。
    """


# ─── 構造化ログ ───────────────────────────────
_logger_initialized = False


def _ensure_log_dir() -> None:
    """events.jsonl の親ディレクトリを保証する (test 環境で STATE_DIR が変わる場合に対応)"""
    EVENTS_LOG.parent.mkdir(parents=True, exist_ok=True)


def log_event(extractor: str, event: str, **fields: Any) -> None:
    """events.jsonl に 1 行 JSON で追記。

    fields はそのまま JSON 化されるため、メトリクス抽出側で
    "extractor=style && event=run_finished の items_written を集計" のような
    クエリが grep + jq で書ける。
    """
    _ensure_log_dir()
    record = {
        "ts": datetime.now().isoformat(timespec="seconds"),
        "extractor": extractor,
        "event": event,
        **fields,
    }
    try:
        with EVENTS_LOG.open("a", encoding="utf-8") as f:
            f.write(json.dumps(record, ensure_ascii=False) + "\n")
    except Exception as e:
        # ログが書けないだけで extractor を止めるのは過剰。stderr に逃がす。
        logging.getLogger("extractor").warning(f"log_event failed: {e}")


@contextmanager
def run_context(extractor: str, **start_fields: Any):
    """run の開始/終了/例外を自動で events.jsonl に記録する with ブロック。

    使い方:
        with run_context("style_extractor", source="all", max_new=8) as ctx:
            # 処理
            ctx["items_written"] = 4

    開始時に run_started, 終了時に run_finished を出力。
    例外時は run_failed を出力してから再 raise。
    ctx に追加したキーは run_finished の fields に含まれる。
    """
    started = time.time()
    log_event(extractor, "run_started", **start_fields)
    ctx: dict[str, Any] = {}
    try:
        yield ctx
    except Exception as e:
        elapsed = round(time.time() - started, 2)
        log_event(
            extractor,
            "run_failed",
            elapsed_sec=elapsed,
            error_class=type(e).__name__,
            error_msg=str(e)[:500],
            **start_fields,
            **ctx,
        )
        raise
    else:
        elapsed = round(time.time() - started, 2)
        log_event(
            extractor,
            "run_finished",
            elapsed_sec=elapsed,
            **start_fields,
            **ctx,
        )


# ─── LiteLLM ─────────────────────────────────
async def call_llm(
    http: httpx.AsyncClient,
    prompt: str,
    model: str = "smart",
    max_tokens: int = 4000,
    temperature: float = 0.2,
    timeout: float = 120.0,
) -> str:
    """単発呼び出し (リトライなし)。後方互換のため残置。

    新規コードは call_llm_with_retry を使うこと。
    """
    resp = await http.post(
        f"{LITELLM_URL}/v1/chat/completions",
        headers={"Authorization": f"Bearer {LITELLM_KEY}"},
        json={
            "model": model,
            "messages": [{"role": "user", "content": prompt}],
            "max_tokens": max_tokens,
            "temperature": temperature,
        },
        timeout=timeout,
    )
    resp.raise_for_status()
    return resp.json()["choices"][0]["message"]["content"]


async def call_llm_with_retry(
    http: httpx.AsyncClient,
    prompt: str,
    model: str = "smart",
    max_tokens: int = 4000,
    temperature: float = 0.2,
    timeout: float = 120.0,
    retries: int = DEFAULT_LLM_RETRIES,
    backoff_base: float = DEFAULT_LLM_BACKOFF_BASE,
    extractor_name: str = "unknown",
) -> str:
    """LLM 呼び出し + 自動リトライ。

    リトライ対象:
    - httpx.TimeoutException / httpx.NetworkError
    - HTTP 5xx / 429
    - レスポンス JSON が壊れている (KeyError/ValueError)

    諦めた場合は LLMContractError を raise する。
    各失敗は events.jsonl に "llm_call_failed" として記録される。
    """
    last_err: Exception | None = None
    for attempt in range(1, retries + 1):
        try:
            resp = await http.post(
                f"{LITELLM_URL}/v1/chat/completions",
                headers={"Authorization": f"Bearer {LITELLM_KEY}"},
                json={
                    "model": model,
                    "messages": [{"role": "user", "content": prompt}],
                    "max_tokens": max_tokens,
                    "temperature": temperature,
                },
                timeout=timeout,
            )
            # 5xx / 429 はリトライ対象
            if resp.status_code >= 500 or resp.status_code == 429:
                last_err = httpx.HTTPStatusError(
                    f"upstream {resp.status_code}", request=resp.request, response=resp
                )
                log_event(
                    extractor_name,
                    "llm_call_failed",
                    attempt=attempt,
                    status_code=resp.status_code,
                    model=model,
                )
            else:
                resp.raise_for_status()
                data = resp.json()
                return data["choices"][0]["message"]["content"]
        except (httpx.TimeoutException, httpx.NetworkError, KeyError, ValueError) as e:
            last_err = e
            log_event(
                extractor_name,
                "llm_call_failed",
                attempt=attempt,
                error_class=type(e).__name__,
                error_msg=str(e)[:200],
                model=model,
            )
        # backoff with jitter
        if attempt < retries:
            sleep_for = backoff_base ** attempt + random.uniform(0, 0.5)
            await asyncio.sleep(sleep_for)
    raise LLMContractError(
        f"LLM call failed after {retries} attempts (model={model}): {last_err}"
    )


def extract_json_block(text: str) -> Any:
    """LLM 出力から JSON だけ取り出す (```json フェンス対応)"""
    s = text.strip()
    if "```json" in s:
        s = s.split("```json", 1)[1].split("```", 1)[0].strip()
    elif s.startswith("```"):
        s = s.split("```", 1)[1].split("```", 1)[0].strip()
    return json.loads(s)


def parse_llm_json_array(
    text: str,
    required_keys: Iterable[str] = (),
    extractor_name: str = "unknown",
) -> list[dict]:
    """LLM 出力を JSON 配列として検証・正規化。

    - フェンス除去
    - 配列であること
    - 各要素が dict で required_keys を持つこと
    検証失敗は LLMContractError を raise (extractor 側で捕捉できる)。

    返り値: dict のリスト (検証済み)。空配列も valid (= "本当に 0 件" を意味する)。
    """
    try:
        data = extract_json_block(text)
    except (json.JSONDecodeError, ValueError) as e:
        log_event(
            extractor_name,
            "llm_parse_failed",
            error_msg=str(e)[:200],
            text_preview=text[:200],
        )
        raise LLMContractError(f"LLM did not return parseable JSON: {e}") from e

    if not isinstance(data, list):
        log_event(
            extractor_name,
            "llm_schema_failed",
            reason="not_a_list",
            type=type(data).__name__,
        )
        raise LLMContractError(
            f"LLM output expected list, got {type(data).__name__}"
        )

    out: list[dict] = []
    skipped = 0
    for i, item in enumerate(data):
        if not isinstance(item, dict):
            skipped += 1
            log_event(
                extractor_name,
                "llm_item_skipped",
                index=i,
                reason="not_a_dict",
                type=type(item).__name__,
            )
            continue
        missing = [k for k in required_keys if k not in item or item[k] in (None, "")]
        if missing:
            skipped += 1
            log_event(
                extractor_name,
                "llm_item_skipped",
                index=i,
                reason="missing_required_keys",
                missing=missing,
            )
            continue
        out.append(item)
    if skipped:
        log_event(
            extractor_name,
            "llm_items_skipped_summary",
            kept=len(out),
            skipped=skipped,
        )
    return out


# ─── Frontmatter ──────────────────────────────
FRONTMATTER_RE = re.compile(r"^---\n(.*?)\n---\n", re.DOTALL)


def parse_frontmatter(content: str) -> tuple[dict, str]:
    """先頭 YAML-ish frontmatter を dict に。残り本文と一緒に返す。"""
    m = FRONTMATTER_RE.match(content)
    if not m:
        return {}, content
    fm_text = m.group(1)
    body = content[m.end():]
    fm: dict = {}
    for line in fm_text.splitlines():
        if ":" not in line:
            continue
        k, _, v = line.partition(":")
        k = k.strip()
        v = v.strip()
        # list literal をざっくり parse
        if v.startswith("[") and v.endswith("]"):
            inner = v[1:-1].strip()
            fm[k] = [x.strip().strip('"').strip("'") for x in inner.split(",") if x.strip()]
        else:
            fm[k] = v.strip('"').strip("'")
    return fm, body


def render_frontmatter(fm: dict) -> str:
    lines = ["---"]
    for k, v in fm.items():
        if isinstance(v, list):
            inner = ", ".join(str(x) for x in v)
            lines.append(f"{k}: [{inner}]")
        else:
            lines.append(f"{k}: {v}")
    lines.append("---")
    return "\n".join(lines) + "\n"


# ─── State ────────────────────────────────────
@dataclass
class ExtractorState:
    """extractor の進行状態。raw のどこまで処理したか etc."""
    name: str
    processed_files: dict[str, str] = field(default_factory=dict)  # path -> sha256 short
    last_run: str = ""
    counters: dict[str, int] = field(default_factory=dict)

    @property
    def state_path(self) -> Path:
        return STATE_DIR / f"{self.name}.json"

    @classmethod
    def load(cls, name: str) -> "ExtractorState":
        p = STATE_DIR / f"{name}.json"
        if p.exists():
            try:
                d = json.loads(p.read_text(encoding="utf-8"))
                return cls(
                    name=name,
                    processed_files=d.get("processed_files", {}),
                    last_run=d.get("last_run", ""),
                    counters=d.get("counters", {}),
                )
            except Exception:
                pass
        return cls(name=name)

    def save(self) -> None:
        self.last_run = datetime.now().isoformat(timespec="seconds")
        STATE_DIR.mkdir(parents=True, exist_ok=True)
        self.state_path.write_text(
            json.dumps(
                {
                    "processed_files": self.processed_files,
                    "last_run": self.last_run,
                    "counters": self.counters,
                },
                ensure_ascii=False,
                indent=2,
            ),
            encoding="utf-8",
        )


# ─── Raw 発見 ─────────────────────────────────
def list_raw_conversations() -> list[Path]:
    d = RAW_DIR / "conversations"
    if not d.exists():
        return []
    return sorted(d.glob("*.md"))


def list_raw_notes(limit: Optional[int] = None) -> list[Path]:
    d = RAW_DIR / "notes"
    if not d.exists():
        return []
    files = sorted(d.glob("*.md"))
    if limit is not None:
        return files[-limit:]
    return files


def list_alignment_files() -> list[Path]:
    """alignment_100_*.md / alignment_60q*.md などまとまった自己回答ノート"""
    d = RAW_DIR / "notes"
    if not d.exists():
        return []
    return sorted(d.glob("alignment_*.md"))


def list_decisions() -> list[Path]:
    d = WIKI_DIR / "decisions"
    if not d.exists():
        return []
    return sorted(d.glob("*.md"))


def list_raw_voice_meetings(limit: Optional[int] = None) -> list[Path]:
    """会議 transcript の生 raw ファイル (Plaud / Recall / Owl 由来)。

    パターン: raw/voice/<source>/YYYY-MM-DD-<slug>.transcript.md
    海山の発言 + 相手発言 + 議題が話者識別付きで入ってる、
    style / judgment / reflex 抽出の主要素材。
    """
    base = RAW_DIR / "voice"
    if not base.exists():
        return []
    files: list[Path] = []
    for source_dir in sorted(base.iterdir()):
        if not source_dir.is_dir():
            continue
        files.extend(sorted(source_dir.glob("*.transcript.md")))
    files.sort()
    if limit is not None:
        return files[-limit:]
    return files


def list_wiki_meetings(limit: Optional[int] = None) -> list[Path]:
    """議事録 wiki ファイル (compile_meeting_note で生成された構造化要約)。

    decisions / action_items / 重要発言が抽出済みなので、
    judgment_extractor の追加ソースとして有効。
    """
    d = WIKI_DIR / "meetings"
    if not d.exists():
        return []
    files = sorted(d.glob("*.md"))
    # recording-policy.md などのメタファイルを除外 (frontmatter type=meeting_note のみ)
    out: list[Path] = []
    for f in files:
        try:
            head = f.read_text(encoding="utf-8", errors="replace")[:500]
            if "type: meeting_note" in head:
                out.append(f)
        except Exception:
            continue
    if limit is not None:
        return out[-limit:]
    return out


def short_hash(content: bytes) -> str:
    import hashlib
    return hashlib.sha256(content).hexdigest()[:12]


# ─── Wiki write helper ────────────────────────
SAFE_ID_RE = re.compile(r"[^a-z0-9-]+")


def safe_id(prefix: str, slug: str, n: int = 0) -> str:
    s = SAFE_ID_RE.sub("-", slug.lower()).strip("-")[:40] or "x"
    if n:
        return f"{prefix}-{s}-{n:03d}"
    return f"{prefix}-{s}"


def next_index(layer_dir: Path, prefix: str) -> int:
    """同じ prefix を持つ既存ファイルの max + 1"""
    if not layer_dir.exists():
        return 1
    max_n = 0
    for f in layer_dir.glob(f"{prefix}-*.md"):
        m = re.search(r"-(\d{3,})\.md$", f.name)
        if m:
            try:
                max_n = max(max_n, int(m.group(1)))
            except ValueError:
                pass
    return max_n + 1


def existing_pattern_summaries(layer_dir: Path, max_files: int = 50) -> list[str]:
    """既存パターンの id + pattern サマリ。LLM に dedup ヒントとして渡す。"""
    out = []
    if not layer_dir.exists():
        return out
    for f in sorted(layer_dir.glob("*.md"))[:max_files]:
        try:
            content = f.read_text(encoding="utf-8")
            fm, _ = parse_frontmatter(content)
            pid = fm.get("id", f.stem)
            pat = fm.get("pattern") or fm.get("trigger") or fm.get("situation") or ""
            if pat:
                out.append(f"- {pid}: {pat}")
        except Exception:
            pass
    return out
