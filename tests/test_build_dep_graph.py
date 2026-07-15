"""build_dep_graph の隔離テスト (AST 依存グラフ生成、2026-07-03)。

tmp_path に git 無しの偽リポジトリを作る → list_py_files は rglob fallback。
repo 実態の import パターン (root絶対 / from-attr / 兄弟 / scripts-prefix / 相対) を再現して解決を検証。
"""
import sys
import pathlib

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / "scripts"))
import build_dep_graph as g  # noqa: E402


def _fake_repo(tmp_path):
    (tmp_path / "a.py").write_text("import b\nfrom pkg.mod import f\n", encoding="utf-8")
    (tmp_path / "b.py").write_text("from pkg import mod\n", encoding="utf-8")
    pkg = tmp_path / "pkg"
    pkg.mkdir()
    (pkg / "__init__.py").write_text("", encoding="utf-8")
    (pkg / "mod.py").write_text("import a\n", encoding="utf-8")           # 循環 a→pkg/mod→a
    (pkg / "sub.py").write_text("from . import mod\nfrom .mod import f\n", encoding="utf-8")
    sc = tmp_path / "scripts"
    sc.mkdir()
    (sc / "util.py").write_text("", encoding="utf-8")
    (sc / "tool.py").write_text("from util import x\n", encoding="utf-8")  # 兄弟解決
    (tmp_path / "top.py").write_text("from tool import y\nimport os, json\n", encoding="utf-8")  # scripts/ prefix
    (tmp_path / "data").mkdir()
    (tmp_path / "data" / "junk.py").write_text("import a\n", encoding="utf-8")   # 除外される
    (tmp_path / "tests").mkdir()
    (tmp_path / "tests" / "t.py").write_text("import a\n", encoding="utf-8")     # 除外される
    return tmp_path


def test_module_id():
    assert g.module_id(pathlib.Path("foo.py")) == "foo"
    assert g.module_id(pathlib.Path("pkg/mod.py")) == "pkg/mod"
    assert g.module_id(pathlib.Path("pkg/__init__.py")) == "pkg"


def test_build_graph_resolution(tmp_path):
    deps, modules = g.build_graph(_fake_repo(tmp_path))
    assert "data/junk" not in modules and "tests/t" not in modules      # 除外
    assert deps["a"] == {"b", "pkg/mod"}                                # 絶対 + from-attr
    assert deps["b"] == {"pkg/mod"}                                     # from pkg import mod → pkg/mod
    assert deps["pkg/mod"] == {"a"}
    assert deps["pkg/sub"] == {"pkg/mod"}                               # 相対 (from . / from .mod)
    assert deps["scripts/tool"] == {"scripts/util"}                     # 兄弟
    assert deps["top"] == {"scripts/tool"}                              # scripts/ prefix
    # 外部 (os, json) はエッジにならない
    all_deps = {d for ds in deps.values() for d in ds}
    assert "os" not in all_deps and "json" not in all_deps


def test_cycles_detected(tmp_path):
    deps, _ = g.build_graph(_fake_repo(tmp_path))
    cycles = g.find_cycles(deps)
    assert ["a", "b", "pkg/mod"] in cycles                              # a→b→pkg/mod→a の 3点循環


def test_prefix_drop_capped_at_one(tmp_path):
    # `from email.mime.text import X` — 内部に email.py があっても 2 段落としの誤解決はしない
    (tmp_path / "email.py").write_text("", encoding="utf-8")
    (tmp_path / "user.py").write_text("from email.mime.text import MIMEText\n", encoding="utf-8")
    deps, _ = g.build_graph(tmp_path)
    assert "user" not in deps                                           # email/mime/text → email は 2 段 = 拒否


def test_render_deterministic(tmp_path):
    deps, modules = g.build_graph(_fake_repo(tmp_path))
    md1 = g.render_markdown(deps, modules)
    md2 = g.render_markdown(deps, modules)
    assert md1 == md2                                                   # 同一入力 = 同一バイト
    assert "自動生成" in md1 and "mermaid" in md1
    assert "`a` → `b`, `pkg/mod`" in md1                                # 隣接リスト
    assert "⇄" in md1                                                   # 循環セクション


def test_self_edge_excluded(tmp_path):
    p = tmp_path / "x.py"
    p.write_text("import x\n", encoding="utf-8")                        # 自己 import (エッジにしない)
    deps, _ = g.build_graph(tmp_path)
    assert "x" not in deps


def test_stdlib_stem_not_resolved_to_sibling(tmp_path):
    # verify-agent 指摘: 兄弟に queue.py がいても `import queue` は stdlib とみなす (偽エッジ防止)
    sc = tmp_path / "an"
    sc.mkdir()
    (sc / "queue.py").write_text("", encoding="utf-8")
    (sc / "worker.py").write_text("import queue\nfrom an import queue as q\n", encoding="utf-8")
    deps, _ = g.build_graph(tmp_path)
    # 絶対形 (from an import queue) は edge、裸 stdlib 形 (import queue) は edge にしない
    assert deps.get("an/worker") == {"an/queue"}


def test_dotdot_import_at_root_boundary(tmp_path):
    # verify-agent 指摘: depth-1 package からの `from .. import X` は top-level X へ解決
    (tmp_path / "a.py").write_text("", encoding="utf-8")
    pkg = tmp_path / "routes"
    pkg.mkdir()
    (pkg / "api.py").write_text("from .. import a\n", encoding="utf-8")
    deps, _ = g.build_graph(tmp_path)
    assert deps.get("routes/api") == {"a"}
