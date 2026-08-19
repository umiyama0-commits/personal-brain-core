"""brain_wiki_helpers/wiki_append.py — wiki 追記 (action="append") の安全化。

★2026-07-27 本番事故の恒久対策 (retrieval 全断の根本原因):
`identity.md` が **375KB / 8,709 行**まで肥大 (同一文 736 回・`## 関連` フッター 295 回) し、
索引時に 800 chunks を生成 → embedding + HNSW 書込でメモリ急増 → **プロセス強制再起動** →
HNSW segment が不整合のまま残り compaction が恒久失敗 → **vector 検索も索引更新も全断**。

原因は compile の append 分岐が `existing + "\n" + content` を**無条件連結**していたこと。
LLM は毎回フル文書 (frontmatter + 本文 + フッター) を返すため、同じ内容が積み上がり続けた。

本 helper は追記前に 3 つの門を通す (pure function = 単体テスト可能):
  1. **frontmatter 剥がし** — 追記側の `---` block は本文中に混ざると区切りの海になる
  2. **重複 skip** — 正規化して既存に含まれていれば追記しない (idempotent)
  3. **サイズ上限** — 上限超過は追記を拒否 (呼び手が loud_fail 通知 = silent 肥大を止める)
"""
from __future__ import annotations

import re

# core wiki (identity/style/thinking) の想定サイズは数十 KB。120KB を超えたら異常肥大とみなす。
DEFAULT_MAX_BYTES = 120_000

_FRONTMATTER_RE = re.compile(r"\A\s*---\s*\n.*?\n---\s*\n", re.S)
_WS_RE = re.compile(r"\s+")


def strip_frontmatter(text: str) -> str:
    """追記される側の先頭 frontmatter を除去 (本文中に `---` を増やさない)。"""
    return _FRONTMATTER_RE.sub("", text or "", count=1)


def _norm(text: str) -> str:
    """空白差を無視した比較用の正規化 (LLM の改行揺れで重複判定を外さない)。"""
    return _WS_RE.sub("", text or "")


def plan_append(existing: str, content: str, *, max_bytes: int = DEFAULT_MAX_BYTES
                ) -> tuple[str | None, str]:
    """追記後の内容を返す。追記すべきでない場合は (None, 理由)。

    Returns:
        (new_text, "ok") — 追記する
        (None, "duplicate") — 既に同内容がある (再 compile の再追記) → 何もしない
        (None, "empty") — 追記内容が実質空
        (None, "size_limit:<bytes>") — 上限超過 (呼び手が loud_fail する)
    """
    body = strip_frontmatter(content)
    if not _norm(body):
        return None, "empty"
    if _norm(body) in _norm(existing or ""):
        return None, "duplicate"
    merged = (existing or "") + "\n" + body
    size = len(merged.encode("utf-8"))
    if size > max_bytes:
        return None, f"size_limit:{size}"
    return merged, "ok"
