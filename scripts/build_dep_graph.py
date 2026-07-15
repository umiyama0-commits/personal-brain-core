#!/usr/bin/env python3
"""scripts/build_dep_graph.py — モジュール依存グラフを AST から決定論的に生成 (★2026-07-03 海山承認)。

背景: オントロジー記事の検討 (2026-07-03) — 「関係を明示するとエージェントの推測ブレが減る」は買い、
ただしコードの関係グラフは LLM に作らせず **AST から決定論的に導出** する (幻覚ゼロ・腐らない)。
出力は `docs/review/dep-graph.md`。ARCHITECTURE.md の「関係の層」+ god object 分割 (#28) の前調査を兼ねる。

設計:
- 対象 = `git ls-files '*.py'` (= 追跡ファイルのみ → 両マシンで同一入力 = 決定論)。data/ と tests/ は除外。
- import 解決は repo の実態パターンに合わせ 4 段: ①絶対 (repo root) ②プレフィックス落とし (from a.b import attr)
  ③同 dir 兄弟 (scripts/analyst/agent.py → playbook) ④scripts/ prefix (sys.path.insert hack)。
  未解決 = 外部 (stdlib/3rd party) として無視。動的 import (importlib 文字列) は対象外 (限界として doc に明記)。
- 出力は無タイムスタンプ・全ソート = 同一入力なら同一バイト (冪等)。内容不変なら書き込みもしない。
- 再生成: pre-commit (MacBook=pre-commit framework / Mac Studio=scripts/git_hooks/pre-commit) が
  .py 変更 commit 時に自動実行 = グラフ鮮度は commit に追随して腐らない。

実行:
  python3 scripts/build_dep_graph.py            # 生成 (変更があれば書き込み) + サマリ表示
  python3 scripts/build_dep_graph.py --quiet    # hook 用 (出力最小)
  python3 scripts/build_dep_graph.py --check    # 鮮度確認のみ (stale なら exit 1、書き込まない)
"""
from __future__ import annotations

import argparse
import ast
import subprocess
import sys
from collections import defaultdict
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
OUT_FILE = ROOT / "docs" / "review" / "dep-graph.md"

EXCLUDE_TOP = {"data", "tests"}          # data=非コード, tests=runtime 依存でないので本グラフから除外
MERMAID_MAX_NODES = 20                   # コア地図の可読上限
_STDLIB = frozenset(getattr(sys, "stdlib_module_names", ()))   # 兄弟解決の stdlib 衝突ガード用


# ─────────────────────── module discovery ───────────────────────
def list_py_files(root: Path) -> list[Path]:
    """git 追跡の .py のみ (両マシンで同一入力=決定論)。git 不在時は rglob fallback。"""
    try:
        out = subprocess.run(["git", "ls-files", "*.py"], cwd=root, capture_output=True,
                             text=True, check=True).stdout
        rels = [Path(p) for p in out.splitlines() if p.strip()]
    except Exception:
        rels = [p.relative_to(root) for p in root.rglob("*.py")]
    return sorted(r for r in rels if r.parts and r.parts[0] not in EXCLUDE_TOP)


def module_id(rel: Path) -> str:
    """foo.py → foo / pkg/mod.py → pkg/mod / pkg/__init__.py → pkg"""
    parts = list(rel.parts)
    if parts[-1] == "__init__.py":
        parts = parts[:-1]
    else:
        parts[-1] = parts[-1][:-3]
    return "/".join(parts)


# ─────────────────────── import extraction ───────────────────────
def extract_import_candidates(src: str, importer_rel: Path) -> list[list[str]]:
    """1 ファイルの AST から候補グループ (= 順序付き代替リスト) を列挙。

    グループ内は「最初に解決した 1 つ」だけをエッジにする (from X import Y の Y がサブモジュールなら
    X/Y のみ、attribute だった時だけ X に fallback — 裸 package への二重エッジを防ぐ):
    - import a.b        → ["a/b"]
    - from a.b import c → ["a/b/c", "a/b"]
    - from . import x   → [(pkg)/x]           (裸 (pkg) はノイズなので出さない)
    - from .m import x  → [(pkg)/m/x, (pkg)/m]
    """
    try:
        tree = ast.parse(src)
    except SyntaxError:
        return []
    groups: list[list[str]] = []
    pkg_parts = list(importer_rel.parts[:-1])  # importer の dir
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for a in node.names:
                groups.append([a.name.replace(".", "/")])
        elif isinstance(node, ast.ImportFrom):
            if node.level and node.level > 0:
                if node.level - 1 > len(pkg_parts):
                    continue  # repo root より上への相対 import (Python 自体が拒否する不正)
                base = pkg_parts[: len(pkg_parts) - (node.level - 1)]
                mod = (node.module or "").split(".") if node.module else []
                stem = "/".join(base + [p for p in mod if p])
                for a in node.names:
                    # stem 空 = depth-1 package からの `from .. import X` → X は top-level module
                    g = [f"{stem}/{a.name}" if stem else a.name]
                    if stem and node.module:
                        g.append(stem)
                    groups.append(g)
            elif node.module:
                m = node.module.replace(".", "/")
                for a in node.names:
                    groups.append([f"{m}/{a.name}", m])
    return groups


def resolve(cand: str, importer_rel: Path, modules: set[str]) -> str | None:
    """候補 (slash 形) を内部モジュールへ解決。4 段 + 右からのプレフィックス落とし。"""
    importer_dir = "/".join(importer_rel.parts[:-1])

    def _try(base: str) -> str | None:
        # プレフィックス落としは 1 段まで: `from X import Y` の X はモジュール必須 (Python 仕様) なので
        # 落としが要るのは「module+alias 候補 (a/b/attr) → a/b」の 1 段のみ。無制限に落とすと
        # 外部 import (email.mime.text 等) の先頭一致で内部モジュールに誤解決する。
        parts = base.split("/")
        for drop in (0, 1):
            if len(parts) - drop < 1:
                break
            m = "/".join(parts[: len(parts) - drop])
            if m in modules:
                return m
        return None

    # ① 絶対 (repo root) + ② プレフィックス落とし
    hit = _try(cand)
    if hit:
        return hit
    # stdlib 名は兄弟/scripts-prefix へ落とさない (例: scripts/analyst/queue.py の隣で
    # `import queue` と書かれても stdlib queue とみなす — 偽エッジ防止, verify-agent 指摘)
    if cand.split("/", 1)[0] in _STDLIB:
        return None
    # ③ 同 dir 兄弟 (sys.path.insert(0, dirname) パターン)
    if importer_dir:
        hit = _try(f"{importer_dir}/{cand}")
        if hit and hit != importer_dir:  # dir 自身 (=自パッケージ) へ縮むのはノイズ
            return hit
    # ④ scripts/ prefix (sys.path.insert(0, ROOT/"scripts") パターン)
    if not cand.startswith("scripts/"):
        hit = _try(f"scripts/{cand}")
        if hit and hit != "scripts":
            return hit
    return None


# ─────────────────────── graph build ───────────────────────
def build_graph(root: Path) -> tuple[dict[str, set[str]], list[str]]:
    """{importer_module: {dep_module}} と全モジュール list を返す。"""
    files = list_py_files(root)
    mod_of = {f: module_id(f) for f in files}
    modules = set(mod_of.values())
    deps: dict[str, set[str]] = defaultdict(set)
    for f in files:
        try:
            src = (root / f).read_text(encoding="utf-8", errors="ignore")
        except OSError:
            continue
        me = mod_of[f]
        for group in extract_import_candidates(src, f):
            for cand in group:                      # グループ内は最初の解決だけ採用
                dep = resolve(cand, f, modules)
                if dep:
                    if dep != me:
                        deps[me].add(dep)
                    break
    return dict(deps), sorted(modules)


def find_cycles(deps: dict[str, set[str]]) -> list[list[str]]:
    """Tarjan SCC — size>1 の強連結成分 (= import 循環) のみ返す。"""
    index: dict[str, int] = {}
    low: dict[str, int] = {}
    on_stack: set[str] = set()
    stack: list[str] = []
    sccs: list[list[str]] = []
    counter = [0]
    sys.setrecursionlimit(10000)

    def strongconnect(v: str) -> None:
        index[v] = low[v] = counter[0]
        counter[0] += 1
        stack.append(v)
        on_stack.add(v)
        for w in sorted(deps.get(v, ())):
            if w not in index:
                strongconnect(w)
                low[v] = min(low[v], low[w])
            elif w in on_stack:
                low[v] = min(low[v], index[w])
        if low[v] == index[v]:
            comp = []
            while True:
                w = stack.pop()
                on_stack.discard(w)
                comp.append(w)
                if w == v:
                    break
            if len(comp) > 1:
                sccs.append(sorted(comp))

    for v in sorted(set(deps) | {d for ds in deps.values() for d in ds}):
        if v not in index:
            strongconnect(v)
    return sorted(sccs)


# ─────────────────────── render ───────────────────────
def _mermaid_id(m: str) -> str:
    return m.replace("/", "_").replace(".", "_").replace("-", "_")


def render_markdown(deps: dict[str, set[str]], modules: list[str]) -> str:
    fan_out = {m: sorted(ds) for m, ds in deps.items()}
    fan_in: dict[str, list[str]] = defaultdict(list)
    for m, ds in deps.items():
        for d in ds:
            fan_in[d].append(m)
    for k in fan_in:
        fan_in[k].sort()
    n_edges = sum(len(v) for v in deps.values())

    # コア地図: degree 上位 MERMAID_MAX_NODES
    degree = {m: len(fan_out.get(m, [])) + len(fan_in.get(m, [])) for m in modules}
    core = [m for m in sorted(modules, key=lambda x: (-degree[x], x)) if degree[m] > 0][:MERMAID_MAX_NODES]
    core_set = set(core)
    mermaid = ["```mermaid", "graph LR"]
    for m in core:
        mermaid.append(f'  {_mermaid_id(m)}["{m}"]')
    for m in core:
        for d in fan_out.get(m, []):
            if d in core_set:
                mermaid.append(f"  {_mermaid_id(m)} --> {_mermaid_id(d)}")
    mermaid.append("```")

    cycles = find_cycles(deps)

    L: list[str] = []
    L.append("# モジュール依存グラフ (自動生成 — 手編集禁止)")
    L.append("")
    L.append("> `python3 scripts/build_dep_graph.py` が **AST から決定論的に生成** (LLM 不使用 = 幻覚ゼロ)。")
    L.append("> 再生成は pre-commit が .py 変更時に自動実行 (= 鮮度は commit に追随)。手動: 上記コマンド。")
    L.append("> 対象: git 追跡 .py (data/ tests/ 除外)。エッジ = 内部モジュール間の import (動的 importlib は対象外)。")
    L.append("> 用途: エージェントが「どこから呼ばれるか/変更の影響範囲」を推測でなくこの表で辿る。詳細背景: ARCHITECTURE.md。")
    L.append("")
    L.append(f"**stats**: modules={len(modules)} / internal edges={n_edges} / cycles(SCC>1)={len(cycles)}")
    L.append("")
    L.append("## コア依存マップ (degree 上位)")
    L.append("")
    L.extend(mermaid)
    L.append("")
    L.append("## 被依存 TOP20 (fan-in = これを変えると壊れる範囲が広い)")
    L.append("")
    L.append("| module | fan-in | 依存してくる側 (抜粋) |")
    L.append("|---|---|---|")
    for m in sorted(fan_in, key=lambda x: (-len(fan_in[x]), x))[:20]:
        who = ", ".join(fan_in[m][:6]) + (" …" if len(fan_in[m]) > 6 else "")
        L.append(f"| `{m}` | {len(fan_in[m])} | {who} |")
    L.append("")
    L.append("## 依存 TOP20 (fan-out = 神オブジェクト/分割候補の指標)")
    L.append("")
    L.append("| module | fan-out | 依存先 (抜粋) |")
    L.append("|---|---|---|")
    for m in sorted(fan_out, key=lambda x: (-len(fan_out[x]), x))[:20]:
        to = ", ".join(fan_out[m][:6]) + (" …" if len(fan_out[m]) > 6 else "")
        L.append(f"| `{m}` | {len(fan_out[m])} | {to} |")
    L.append("")
    L.append("## import 循環 (SCC>1 — リファクタ最優先候補)")
    L.append("")
    if cycles:
        for c in cycles:
            L.append(f"- {' ⇄ '.join(f'`{m}`' for m in c)}")
    else:
        L.append("- なし ✅")
    L.append("")
    L.append("## 全エッジ (importer → 内部依存)")
    L.append("")
    L.append("<details><summary>全モジュールの隣接リスト (クリックで展開)</summary>")
    L.append("")
    for m in sorted(fan_out):
        L.append(f"- `{m}` → {', '.join(f'`{d}`' for d in fan_out[m])}")
    L.append("")
    L.append("</details>")
    L.append("")
    return "\n".join(L)


# ─────────────────────── main ───────────────────────
def main() -> int:
    ap = argparse.ArgumentParser(description="AST からモジュール依存グラフを生成")
    ap.add_argument("--quiet", action="store_true", help="hook 用: 出力最小")
    ap.add_argument("--check", action="store_true", help="鮮度確認のみ (stale なら exit 1)")
    a = ap.parse_args()

    deps, modules = build_graph(ROOT)
    md = render_markdown(deps, modules)
    current = OUT_FILE.read_text(encoding="utf-8") if OUT_FILE.exists() else ""

    if a.check:
        if current != md:
            print(f"❌ {OUT_FILE.relative_to(ROOT)} が stale — python3 scripts/build_dep_graph.py で再生成を")
            return 1
        print("✅ dep-graph は最新")
        return 0

    if current == md:
        if not a.quiet:
            print(f"変更なし: {OUT_FILE.relative_to(ROOT)}")
        return 0
    OUT_FILE.parent.mkdir(parents=True, exist_ok=True)
    OUT_FILE.write_text(md, encoding="utf-8")
    if not a.quiet:
        n_edges = sum(len(v) for v in deps.values())
        print(f"✅ 生成: {OUT_FILE.relative_to(ROOT)} (modules={len(modules)}, edges={n_edges})")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
