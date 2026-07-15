"""
brain_wiki_helpers/recency_bias.py — vector hits の last_updated 重み付け rerank

★2026-05-22 Phase 1b 切り出し:
brain_wiki.BrainWiki._apply_recency_weight を pure function 化。
self.WIKI_DIR 依存を wiki_dir 引数に変更。
"""
from __future__ import annotations

import re
from datetime import date as _date, datetime as _dt
from pathlib import Path
from typing import Optional


def _parse_last_updated(content: str) -> Optional[_date]:
    """frontmatter から更新日付を抽出。なければ None。

    ★2026-06-08 システム評価 1-4 (DA cross-check 発見): `last_updated:` だけでなく
    `updated:` も読む。実 corpus では判断系 (decisions/*: 17 中 16、top-level style.md)
    が `updated:` キーを使っており、`last_updated:` 限定だと「最新の判断軸を最優先」の
    recency が最も効かせたい層で multiplier=1.00 の no-op になっていた。
    両方ある場合は `last_updated:` を優先。
    """
    m = re.match(r"^---\n(.*?)\n---\n", content, re.DOTALL)
    if not m:
        return None
    primary = None    # last_updated:
    fallback = None   # updated:
    for line in m.group(1).splitlines():
        s = line.strip()
        if s.startswith("last_updated:"):
            primary = line.split(":", 1)[1].strip().strip('"').strip("'")
        elif s.startswith("updated:"):
            fallback = line.split(":", 1)[1].strip().strip('"').strip("'")
    val = primary if primary is not None else fallback
    if not val:
        return None
    for fmt in ("%Y-%m-%d", "%Y/%m/%d", "%Y.%m.%d"):
        try:
            return _dt.strptime(val, fmt).date()
        except Exception:
            pass
    return None


def _recency_multiplier(days: int) -> float:
    """経過日数に応じた重み係数 (★2026-05-22 海山指示の階段表)。

      7 日以内    : ×1.05  (= 最新の判断軸を最優先)
      7-14 日     : ×1.02
      14-30 日    : ×1.00  (= 基準、ニュートラル)
      30-90 日    : ×0.97
      90-180 日   : ×0.93
      180-365 日  : ×0.85
      365+ 日     : ×0.70
    """
    if days < 0:
        return 1.00  # 未来日付は基準
    if days <= 7:
        return 1.05
    if days <= 14:
        return 1.02
    if days <= 30:
        return 1.00
    if days <= 90:
        return 0.97
    if days <= 180:
        return 0.93
    if days <= 365:
        return 0.85
    return 0.70


# ★2026-06-08 評価 1-4: Cohere rerank がある hit 群は relevance を主キーにし、relevance
# 差がこの ε 以内の near-tie のみ recency (last_updated 新しい順) で並べ替える。
# magic な乗算 strength を排し「明確な relevance 差は recency で覆らない (factual 安全) /
# 拮抗する判断 doc は最新版が勝つ (judgment 意図)」を両立する (Fact-checker + DA 推奨設計)。
RERANK_TIEBREAK_EPS = 0.05


def _safe_float(v, default: float) -> float:
    try:
        return float(v)
    except Exception:
        return default


def apply_recency_weight(hits: list, wiki_dir: Path) -> list:
    """vector search hits を更新日付で rerank。

    ★2026-06-08 評価 1-4 (cross-check 3種で収束した設計):
    - rerank_score がある場合 (= Cohere rerank 発火): relevance を**主キー**とし、relevance 差が
      RERANK_TIEBREAK_EPS 以内の near-tie のみ last_updated 新しい順で並べ替える。明確な
      relevance 差は recency で覆らない (factual 安全) / 拮抗する判断 doc は最新版が勝つ。
      従来は rerank 後でも distance で全件再 sort し relevance を捨てていた (= 関連薄+新しい
      doc が rerank #1 を leapfrog するバグ) のを是正。
    - rerank_score が無い場合 (Cohere 無効/失敗/hits<=10): 従来どおり
      (1 - distance) × multiplier (distance あり) / rank × multiplier (distance 無し)。

    Args:
        hits: list of dict、各 dict は少なくとも "source" (= wiki path) を持つ。
              optional: "rerank_score" (Cohere relevance)、"distance" (Chroma cosine)。
        wiki_dir: WIKI_DIR の Path。各 hit の source を解決するのに使う。

    Returns:
        rerank された hits の新 list。
    """
    if not hits:
        return hits
    today = _date.today()
    n = len(hits)

    # 各 hit の更新日付 + multiplier を解決 (file 読みは 1 回だけ)
    enriched = []  # (orig_index, hit, last_date_or_None, multiplier)
    for i, h in enumerate(hits):
        last = None
        multiplier = 1.00
        src = (h.get("source") or "").replace("wiki/", "")
        if src:
            fpath = wiki_dir / src
            if not fpath.exists():
                matches = list(wiki_dir.rglob(Path(src).name))
                fpath = matches[0] if len(matches) == 1 else None
            if fpath is not None:
                try:
                    content = fpath.read_text(encoding="utf-8")
                    last = _parse_last_updated(content)
                    if last is not None:
                        multiplier = _recency_multiplier((today - last).days)
                except Exception:
                    pass
        enriched.append((i, h, last, multiplier))

    rr_present = any(h.get("rerank_score") is not None for _, h, _, _ in enriched)

    if rr_present:
        # relevance 主キー (EPS バケット量子化) → 同 bucket 内は last_updated 新しい順。
        # 日付不明 (ordinal 0) は同 bucket 内で最後尾。i は安定 tie-break (= rerank 順)。
        def _rerank_key(e):
            i, h, last, _m = e
            base = _safe_float(h.get("rerank_score"), 0.5)
            bucket = round(base / RERANK_TIEBREAK_EPS)
            recency_ord = last.toordinal() if last is not None else 0
            return (-bucket, -recency_ord, i)
        key_fn = _rerank_key
    else:
        # 従来挙動: (1 - distance) × multiplier、distance 無しは rank × multiplier
        def _distance_key(e):
            i, h, _last, mult = e
            dist = h.get("distance")
            if dist is not None:
                sim = max(0.0, 1.0 - _safe_float(dist, 0.5))
                score = sim * mult
            else:
                score = (1.0 - i / max(1, n)) * mult
            return (-score, i)
        key_fn = _distance_key

    enriched.sort(key=key_fn)
    return [h for _, h, _, _ in enriched]
