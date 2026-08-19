"""import_dedup.py — 取り込みファイルの「中身が前回と同じなら再 compile しない」判定 (pure)。

★2026-08-03 コスト実測 (docs/decisions/2026-08-03-silent-fallback-cost-leak.md):
LINE Works スクレイパーは 2 時間おきに全 13 ルームの会話を **その時点の全文** で書き出すため、
新規発言が無いルームも毎回 IMPORT_DIR に落ちる。watcher はそれを毎回 LLM compile しており、
1 call ~44K token / $0.069、8 サイクル×13 ルーム/日 で支出の約 4 割を占めていた。

★初版の致命的バグ (§1.15 cross-check の Reviewer / DA が実データで独立検出、実装前に修正):
初版は「時刻を含む行」を丸ごと除外していたが、LINE Works の**発言行そのもの**が
`19:34<TAB>海山丈司<TAB>本文` 形式のため、**全発言が fingerprint から消える**。実測で
「新規発言があるのに 61% が永久 skip」= 知識の恒久欠落になっていた (626 file で検証)。

現行方針 (fail-open を徹底):
- 行頭のタイムスタンプ「だけ」を除去し、**発言本文は必ず残す**。
- 日付トークンは行内から除去 (ヘッダの日付変化で fingerprint が動かないように)。
- **行数を併記して保存**し、行数が増えていたら fingerprint 一致でも skip しない
  (新規発言は行数を増やすため、取りこぼしの二重防止)。
- 有意行が少なすぎる (= 正規化で中身が消えた) 場合は fingerprint を返さず **常に compile**。
- state 破損・判定不能も compile 側 (skip しない)。
- chromadb は引かない (§1.5 の並行アクセス禁止に触れないよう、完全一致のみ)。
"""
from __future__ import annotations

import hashlib
import json
import re
from pathlib import Path

# 行頭のタイムスタンプ (「19:34」「[10:58]」「09:05:12」) — **除去するのは接頭辞だけ**。
_TS_PREFIX_RE = re.compile(r"^[\s　]*\[?\d{1,2}:\d{2}(?::\d{2})?\]?[\s　]*")
# 行内の日付トークン (2026-07-17 / 2026年7月17日 / 2026/07/17) — スクレイプ日で毎回変わる。
_DATE_TOKEN_RE = re.compile(r"\d{4}[-/年]\s?\d{1,2}[-/月]\s?\d{1,2}日?")
# 取得メタ行 (行全体が揮発情報) のみ除外。
_META_LINE_RE = re.compile(
    r"^\s*(取得日時|取得日|scraped_at|generated|Generated|更新日時|exported_at)\b",
    re.IGNORECASE,
)
# 正規化後にこの行数未満なら「本文を取り違えている」とみなし dedup しない (fail-open)。
MIN_SIGNIFICANT_LINES = 3


def _normalized_lines(text: str) -> list[str]:
    out: list[str] = []
    for ln in (text or "").splitlines():
        if _META_LINE_RE.search(ln):
            continue
        ln = _TS_PREFIX_RE.sub("", ln)      # 時刻接頭辞だけ落として本文は残す
        ln = _DATE_TOKEN_RE.sub("", ln)     # 行内の日付は除去
        ln = ln.strip()
        if ln:
            out.append(ln)
    return out


def body_fingerprint(text: str) -> str:
    """本文の sha256 (先頭 16 桁)。判定不能なら "" (= dedup しない)。"""
    lines = _normalized_lines(text)
    if len(lines) < MIN_SIGNIFICANT_LINES:
        return ""  # fail-open: 中身が取れていない → 必ず compile
    return hashlib.sha256("\n".join(lines).encode("utf-8", "ignore")).hexdigest()[:16]


def body_line_count(text: str) -> int:
    return len(_normalized_lines(text))


def dedup_key(filename: str) -> str:
    """日付サフィックスのみ除いた安定キー。

    末尾の日付だけを剥がす (初版は `.*$` で貪欲に消し、「売上20260801共有」→「売上」のように
    ルーム名を潰して別ルームと衝突し得た = DA 指摘)。
    """
    stem = Path(filename).stem
    return re.sub(r"[_-]?\d{4}[-_]?\d{1,2}[-_]?\d{1,2}$", "", stem) or stem


def is_duplicate(state: dict, filename: str, text: str) -> bool:
    """前回 compile 時と本文が同一か。state は {key: {"fp":..., "lines":N}}。

    fail-open 条件 (いずれも False = compile する):
      - fingerprint が空 (正規化で本文が消えた / 極小ファイル)
      - 記録が無い / 形式が違う
      - **行数が増えている** (新規発言があるのに hash 衝突、という最悪ケースの保険)
    """
    try:
        fp = body_fingerprint(text)
        if not fp:
            return False
        prev = state.get(dedup_key(filename))
        if not isinstance(prev, dict):
            return False
        if prev.get("fp") != fp:
            return False
        if body_line_count(text) > int(prev.get("lines", 0)):
            return False
        return True
    except Exception:
        return False


def remember(state: dict, filename: str, text: str) -> dict:
    """compile 実行後に fingerprint と行数を記録して state を返す (呼び出し側が保存)。"""
    try:
        fp = body_fingerprint(text)
        if fp:
            state[dedup_key(filename)] = {"fp": fp, "lines": body_line_count(text)}
    except Exception:
        pass
    return state


def load_state(path: Path) -> dict:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def save_state(path: Path, state: dict) -> None:
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(state, ensure_ascii=False, indent=1), encoding="utf-8")
    except Exception:
        pass
