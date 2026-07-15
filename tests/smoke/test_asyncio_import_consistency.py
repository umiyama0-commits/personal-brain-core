"""smoke test: 「asyncio.xxx 使う module には import asyncio 必須」を AST で強制

★2026-05-26 海山指示「import 漏れの regression を AST で保証」.
背景: e4c50fa で asyncio.gather/to_thread 使ったのに import asyncio 漏れ
       → 全 turn deterministic に NameError → revert 経歴あり.
       grep-based test だと検知できなかった盲点を AST で塞ぐ.

ロジック:
  全 .py を walk → ast.Attribute で `asyncio.xxx` 参照を検出
  → 使ってる module には `import asyncio` (= ast.Import で alias.name == "asyncio")
     又は `from asyncio import xxx` (= ast.ImportFrom で module == "asyncio") のいずれかが必須
"""
from __future__ import annotations

import ast
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent.parent

# walk から除外する path (= 第三者 code / venv / data 等)
_EXCLUDE_PARTS = {
    ".venv", "venv", "node_modules", ".git",
    "data", ".pytest_cache", "__pycache__",
    # tests 自体は意図的に skip (= test 内で asyncio 等は別 fixture で扱う)
    "tests",
}


def _uses_asyncio_module(tree: ast.AST) -> bool:
    """ast tree 内に `asyncio.xxx` 形式の attribute 参照があるか."""
    for node in ast.walk(tree):
        if isinstance(node, ast.Attribute):
            v = node.value
            if isinstance(v, ast.Name) and v.id == "asyncio":
                return True
    return False


def _has_asyncio_import(tree: ast.AST) -> bool:
    """ast tree 内に `import asyncio` 又は `from asyncio import ...` があるか."""
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                if alias.name == "asyncio":
                    return True
        elif isinstance(node, ast.ImportFrom):
            if node.module == "asyncio":
                return True
    return False


def _iter_py_files():
    """REPO 内の .py を yield (= 除外 path を除く)."""
    for py in REPO.rglob("*.py"):
        try:
            rel = py.relative_to(REPO)
        except ValueError:
            continue
        if any(part in _EXCLUDE_PARTS for part in rel.parts):
            continue
        yield py, rel


@pytest.mark.smoke
def test_brain_wiki_imports_asyncio():
    """brain_wiki.py が asyncio を使う + import asyncio がある (= e4c50fa 再発防止)."""
    src = (REPO / "brain_wiki.py").read_text(encoding="utf-8")
    tree = ast.parse(src)
    assert _uses_asyncio_module(tree), "brain_wiki.py が asyncio module を使ってない"
    assert _has_asyncio_import(tree), (
        "brain_wiki.py が asyncio.xxx 使ってるのに `import asyncio` 無し → NameError 確定 "
        "(= e4c50fa の retry regression)"
    )


@pytest.mark.smoke
def test_all_repo_files_with_asyncio_have_import():
    """全 source .py を walk: asyncio 使ってる module には import asyncio 必須."""
    failures = []
    for py, rel in _iter_py_files():
        try:
            src = py.read_text(encoding="utf-8")
            tree = ast.parse(src)
        except (SyntaxError, UnicodeDecodeError, OSError):
            # parse 失敗は別 test で carbon、ここでは skip
            continue
        if not _uses_asyncio_module(tree):
            continue
        if not _has_asyncio_import(tree):
            failures.append(str(rel))
    assert not failures, (
        f"{len(failures)} module(s) で asyncio.xxx 使ってるのに import asyncio 漏れ:\n"
        + "\n".join(f"  - {f}" for f in failures)
    )


# ─── parallel_load の挙動 / flag check ─────
@pytest.mark.smoke
def test_clone_respond_has_parallel_flag_gate():
    """PARALLEL_LOAD_ENABLED flag で sequential / parallel 切替 (default OFF、★2026-05-26 21:40 緊急revert)."""
    src = (REPO / "brain_wiki.py").read_text(encoding="utf-8")
    # env flag check
    assert 'os.getenv("PARALLEL_LOAD_ENABLED"' in src
    # ★default OFF (= 21:40 bot 沈黙報告で safety side に戻し)
    # env=1 設定で 海山が安定確認後 ON 切替可能
    assert 'getenv("PARALLEL_LOAD_ENABLED", "0")' in src
    # parallel path (= flag ON 時のみ)
    assert "clone_respond.parallel_load" in src
    assert "return_exceptions=True" in src
    # sequential path (= default、safety)
    assert "clone_respond.sequential_load" in src


@pytest.mark.smoke
def test_clone_respond_unpacks_results_with_exception_fallback():
    """各 result が exception の場合に空 fallback (= 1 つ失敗で全 discard 回避、Fact-checker 推奨)."""
    src = (REPO / "brain_wiki.py").read_text(encoding="utf-8")
    # destructure with isinstance check
    assert "isinstance(results[0], BaseException)" in src
    assert "isinstance(results[1], BaseException)" in src
    assert "isinstance(results[2], BaseException)" in src
    assert "isinstance(results[3], BaseException)" in src
    # group は tuple unpacking + 例外時に ("", "") fallback
    assert 'group_context_block, group_instruction_block = "", ""' in src


@pytest.mark.smoke
def test_clone_respond_uses_4_loader_helpers():
    """4 helper の存在 (= drive / memory / group / few_shot)."""
    src = (REPO / "brain_wiki.py").read_text(encoding="utf-8")
    assert "async def _load_drive_block" in src
    assert "async def _load_user_memory_block" in src
    assert "async def _load_group_blocks" in src
    assert "async def _load_few_shot" in src
    # 各 helper が asyncio.to_thread で blocking I/O wrap
    assert "asyncio.to_thread(_build_drive_ctx" in src
    assert "asyncio.to_thread(" in src  # 一般的 to_thread 使用
    assert "asyncio.to_thread(self._load_few_shot_examples)" in src
