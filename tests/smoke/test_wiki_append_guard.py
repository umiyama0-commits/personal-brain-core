"""tests/smoke/test_wiki_append_guard.py

★2026-07-27 retrieval 全断事故の恒久対策を固定するテスト。

事故: compile の append 分岐が `existing + content` を無条件連結していたため、LLM が毎回返す
フル文書 (frontmatter + 本文 + フッター) が積み上がり identity.md が 375KB / 8,709 行に肥大
(同一文 736 回・`## 関連` フッター 295 回) → 索引で 800 chunks → メモリ急増でプロセス強制再起動
→ HNSW segment 破損 → vector 検索と索引更新が全断 (約 12 時間、176 エラー)。
"""
from __future__ import annotations

from pathlib import Path

_ROOT = Path(__file__).resolve().parents[2]


def test_strip_frontmatter():
    from brain_wiki_helpers.wiki_append import strip_frontmatter
    doc = "---\nupdated: 2026-07-27\ntags: [a]\n---\n# 本文\nあいうえお\n"
    out = strip_frontmatter(doc)
    assert out.startswith("# 本文") and "updated:" not in out
    # frontmatter 無しはそのまま
    assert strip_frontmatter("# そのまま") == "# そのまま"


def test_duplicate_append_is_skipped():
    """★事故の中核: 同じ内容の再 compile で無限に積み上がらない。"""
    from brain_wiki_helpers.wiki_append import plan_append
    existing = "---\nupdated: 2026-07-26\n---\n# Identity\n- デジタルツールを活用している。\n"
    same = "---\nupdated: 2026-07-27\n---\n- デジタルツールを活用している。\n"
    merged, reason = plan_append(existing, same)
    assert merged is None and reason == "duplicate"
    # 空白/改行の揺れがあっても重複と判定する
    merged2, reason2 = plan_append(existing, "-  デジタルツールを活用している。\n\n")
    assert merged2 is None and reason2 == "duplicate"


def test_new_content_is_appended_without_frontmatter():
    from brain_wiki_helpers.wiki_append import plan_append
    existing = "---\nupdated: 2026-07-26\n---\n# Identity\n- 既存の記述。\n"
    new = "---\nupdated: 2026-07-27\n---\n- 新しい価値観の記述。\n"
    merged, reason = plan_append(existing, new)
    assert reason == "ok" and merged is not None
    assert "新しい価値観の記述" in merged
    # 追記側の frontmatter は本文に混ざらない (`---` の海を作らない)
    assert merged.count("---") == existing.count("---")


def test_size_limit_refuses_runaway_growth():
    """★上限を超えたら追記を拒否 (呼び手が loud_fail)。事故の再発を構造で止める。"""
    from brain_wiki_helpers.wiki_append import plan_append
    huge = "x" * 130_000
    merged, reason = plan_append(huge, "- 追加したい記述\n")
    assert merged is None and reason.startswith("size_limit")


def test_empty_content_skipped():
    from brain_wiki_helpers.wiki_append import plan_append
    assert plan_append("既存", "---\nupdated: x\n---\n\n  \n")[1] == "empty"


def test_wired_into_compile_write_path():
    """append 分岐が「3 門 + loud_fail」で配線されていること。

    ★2026-08-18: 固定長の窓 (1800 字) で見ていたため、上限超過時の overflow 退避を
    足した分だけ loud_fail が窓の外に出て落ちた。窓幅ではなく **分岐の終端**
    (次のメソッド定義) までを見るようにして、実装が伸びても壊れないようにする。
    """
    src = (_ROOT / "brain_wiki.py").read_text(encoding="utf-8")
    i = src.index('elif action == "append":')
    end = src.index("    @staticmethod", i)
    seg = src[i:end]
    assert "plan_append" in seg, "append 分岐が安全化されていない"
    assert "existing + \"\\n\" + content" not in seg, "無条件連結が残っている (事故の再発経路)"
    assert "loud_fail" in seg, "上限超過が silent (§1.18)"
