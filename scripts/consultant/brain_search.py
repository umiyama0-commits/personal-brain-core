"""scripts/consultant/brain_search.py — search_brain のバックエンド(chromadb 非接触).

★2026-06-20。戦略アナリストの search_brain は **コンパイル済み wiki markdown を直接読む**。
理由(cross-check Reviewer §1.5): host から 2 つ目の chromadb client を開くと本番 line-bot と
並行アクセスして SIGSEGV crash loop(docs/decisions/2026-04-27-chromadb-concurrent-access-ban.md)。
analyst registry.py と同じ「stdlib のみ・純粋・ディスク直読」流儀で、本番 index に一切触れない。

keyword + CJK 2-gram の軽量スコア検索。意味検索が要れば将来 別パス(bot 非共有)で。
"""
from __future__ import annotations

import re
from collections import Counter
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
WIKI_DIR = ROOT / "data" / "brain" / "wiki"

# ★2026-07-03 (v3 ADR DA R6): 深層 private (personal/ + interview/) の除外判定。
# 単一定義は brain_wiki_helpers/domain.py の is_deep_private_rel だが、package __init__ が
# httpx を引き込むため sandbox/別 root では import 不可があり得る → stdlib fallback で
# **同じ集合** を fail-safe に除外。fallback は常時定義の名前付き関数にして
# tests/smoke/test_deep_private_domain.py が domain.py 定義との drift を直接検証する
# (DEEP_PRIVATE_DIRS に dir を足したらここも足すこと)。
def _deep_private_fallback(rel) -> bool:
    try:
        return Path(rel).parts[:1] in (("personal",), ("interview",))
    except Exception:
        return False


try:
    import sys as _sys
    if str(ROOT) not in _sys.path:
        _sys.path.insert(0, str(ROOT))
    from brain_wiki_helpers.domain import is_deep_private_rel as _is_deep_private_rel
except Exception:
    _is_deep_private_rel = _deep_private_fallback

_ASCII = re.compile(r"[A-Za-z0-9_]{2,}")
_CJK = re.compile(r"[぀-ヿ㐀-鿿豈-﫿]{2,}")


def _terms(text: str) -> list[str]:
    """ascii 語(len>=2)+ CJK 連の 2-gram を抽出(語境界の無い日本語向け)。"""
    terms: list[str] = [m.group(0) for m in _ASCII.finditer(text.lower())]
    for m in _CJK.finditer(text):
        run = m.group(0)
        terms.extend(run[i:i + 2] for i in range(len(run) - 1))
    return terms


def _score(doc: str, source: str, q_terms: set[str]) -> int:
    body = doc.lower()
    src = source.lower()
    s = 0
    for t in q_terms:
        s += body.count(t)
        s += 5 * src.count(t)        # ファイル名/パス一致は強い手がかり
    return s


def _excerpt(doc: str, q_terms: set[str], max_chars: int) -> str:
    low = doc.lower()
    hits = [low.find(t) for t in q_terms if low.find(t) >= 0]
    pos = min(hits) if hits else -1
    if pos < 0:
        return doc[:max_chars].strip()
    start = max(0, pos - 120)
    end = start + max_chars
    return ("…" if start > 0 else "") + doc[start:end].strip() + ("…" if end < len(doc) else "")


def search(query: str, *, k: int = 5, max_chars: int = 900, wiki_dir: Path = WIKI_DIR) -> list[dict]:
    """wiki markdown を keyword スコアで検索。[{source, score, excerpt}] を score 降順で返す。"""
    q_terms = set(_terms(query or ""))
    if not q_terms or not wiki_dir.exists():
        return []
    scored: list[tuple[int, str, str]] = []
    for f in wiki_dir.rglob("*.md"):
        # ★2026-06-28 personal / ★2026-07-03 interview (v3 ADR DA R6): コンサル (OWNDAYS 戦略) は
        #   深層 private (非OWNDAYS PJ + 人格深層) を読まない。判定は冒頭の _is_deep_private_rel。
        if _is_deep_private_rel(f.relative_to(wiki_dir)):
            continue
        try:
            doc = f.read_text(encoding="utf-8")
        except Exception:
            continue
        rel = str(f.relative_to(wiki_dir))
        sc = _score(doc, rel, q_terms)
        if sc > 0:
            scored.append((sc, rel, doc))
    scored.sort(key=lambda x: x[0], reverse=True)
    return [{"source": rel, "score": sc, "excerpt": _excerpt(doc, q_terms, max_chars)}
            for sc, rel, doc in scored[:k]]


def format_results(query: str, results: list[dict]) -> str:
    """tool result 用に整形。無ヒット時は『外部で補わず要・外部確認』を明示。"""
    if not results:
        return (f"「{query}」に該当する社内 wiki は見つかりませんでした。"
                "外部知識で勝手に補わず、必要なら『要・外部確認』として扱うこと。")
    parts = [f"## 社内 wiki 検索: 「{query}」(上位 {len(results)} 件)"]
    for i, r in enumerate(results, 1):
        parts.append(f"### [{i}] {r['source']}\n{r['excerpt']}")
    return "\n\n".join(parts)


def sections_catalog(wiki_dir: Path = WIKI_DIR, limit: int = 40) -> str:
    """system prompt 用: wiki のトップ階層と件数の軽い目次(search_brain で何が引けるか)。"""
    if not wiki_dir.exists():
        return "(wiki 未検出)"
    c: Counter = Counter()
    for f in wiki_dir.rglob("*.md"):
        rel = f.relative_to(wiki_dir)
        # ★2026-06-28 personal / ★2026-07-03 interview (v3 ADR DA R6): catalog にも深層 private を
        #   出さない (存在自体を OWNDAYS に見せない)
        if _is_deep_private_rel(rel):
            continue
        top = rel.parts[0] if len(rel.parts) > 1 else "(root)"
        c[top] += 1
    items = sorted(c.items(), key=lambda x: x[1], reverse=True)[:limit]
    return ", ".join(f"{k}({n})" for k, n in items)
