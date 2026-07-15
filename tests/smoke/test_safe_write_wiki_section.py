"""smoke test: clone_improve_lib の section 置換 + overwrite guard (★2026-06-07 エージェント評価)

旧 replace_section→overwrite map が LLM の section content で wiki **全文を消失** させていた data-loss
バグの修正。真の section 置換 + overwrite 激減 guard の回帰保護。destructive (wiki write) なので必須。
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(REPO / "scripts"))

import clone_improve_lib as lib  # noqa: E402


@pytest.mark.smoke
def test_replace_section_preserves_others(tmp_path):
    """対象 section のみ置換、他 section・Title は完全保持 (全文消失しない)。"""
    f = tmp_path / "t.md"
    f.write_text("# T\n\n## A\n旧A\n\n## B\nB保持\n\n## C\nC保持\n", encoding="utf-8")
    assert lib._replace_section(f, "## A", "## A\n新A") is True
    txt = f.read_text(encoding="utf-8")
    assert "新A" in txt and "旧A" not in txt
    assert "B保持" in txt and "C保持" in txt and "# T" in txt


@pytest.mark.smoke
def test_replace_section_includes_subsection(tmp_path):
    """section 配下の subsection (### 〜) も section の一部として置換、次の同 level 見出しで止まる。"""
    f = tmp_path / "t.md"
    f.write_text("## A\n本文\n### A-sub\nサブ\n## B\nB\n", encoding="utf-8")
    lib._replace_section(f, "## A", "## A\n新本文")
    txt = f.read_text(encoding="utf-8")
    assert "新本文" in txt and "A-sub" not in txt
    assert "## B" in txt


@pytest.mark.smoke
def test_replace_section_anchor_not_found_no_overwrite(tmp_path):
    """anchor 未発見なら False を返し、ファイルは一切変更しない (全文上書きしない)。"""
    f = tmp_path / "t.md"
    orig = "## A\n本文\n"
    f.write_text(orig, encoding="utf-8")
    assert lib._replace_section(f, "## 無い見出し", "x") is False
    assert f.read_text(encoding="utf-8") == orig


@pytest.mark.smoke
def test_replace_section_preserves_heading_if_content_omits_it(tmp_path):
    """content が見出しを含まなくても anchor 見出しを保持。"""
    f = tmp_path / "t.md"
    f.write_text("## A\n旧\n## B\nB\n", encoding="utf-8")
    lib._replace_section(f, "## A", "新本文のみ")
    txt = f.read_text(encoding="utf-8")
    assert "## A" in txt and "新本文のみ" in txt and "## B" in txt


@pytest.mark.smoke
def test_overwrite_shrink_guard(tmp_path, monkeypatch):
    """overwrite mode: 激減 (50%未満) は拒否しファイル保持、微減は採用。"""
    monkeypatch.setattr(lib, "wiki_path", lambda rel: tmp_path / rel)
    big = "x" * 2000
    (tmp_path / "f.md").write_text(big, encoding="utf-8")
    assert lib.safe_write_wiki("f.md", "y" * 200, mode="overwrite") is False
    assert (tmp_path / "f.md").read_text(encoding="utf-8") == big, "激減は拒否しファイル保持"
    assert lib.safe_write_wiki("f.md", "y" * 1500, mode="overwrite") is True


@pytest.mark.smoke
def test_safe_write_wiki_replace_section_mode(tmp_path, monkeypatch):
    """safe_write_wiki(mode='replace_section') が anchor section のみ置換。"""
    monkeypatch.setattr(lib, "wiki_path", lambda rel: tmp_path / rel)
    (tmp_path / "f.md").write_text("## A\n旧\n## B\nB\n", encoding="utf-8")
    ok = lib.safe_write_wiki("f.md", "## A\n新", mode="replace_section", section_anchor="## A")
    assert ok is True
    txt = (tmp_path / "f.md").read_text(encoding="utf-8")
    assert "新" in txt and "旧" not in txt and "## B" in txt
