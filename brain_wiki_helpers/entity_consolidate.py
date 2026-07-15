"""brain_wiki_helpers/entity_consolidate.py — 過剰分割の恒久対策(★2026-07-01)。

LLM compile が `decisions/YYYY-MM-DD-{slug}.md` のように日付プレフィックス付きパスを出力すると、
同一エンティティの日次更新が毎日 **新規ファイル** として増殖する(ff-cvr 22件等)。
本 helper は compile 出力を **entity-keyed の単一ページ({slug}.md)+ 時系列ログ**へ正規化する。
_apply_update から呼ばれる純粋関数(I/O は caller の callback 経由=テスト容易)。

対象は decisions/ projects/ のみ(meetings/ は各回が独立イベント=日付ファイルが正しいので除外)。
"""
from __future__ import annotations
import re

DATED_STEM = re.compile(r'^(20\d{2}-\d{2}-\d{2})-(.+)$')
CONSOLIDATE_DIRS = ("decisions", "projects")


def _strip_fm_and_title(content: str) -> str:
    """content から frontmatter と先頭 '# title' を除いた本文を返す。"""
    m = re.match(r'^---\n.*?\n---\n(.*)$', content, re.DOTALL)
    body = m.group(1) if m else content
    body = re.sub(r'^\s*#\s+.+\n', '', body, count=1)
    return body.strip()


def _extract_fm(content: str) -> str:
    m = re.match(r'^---\n(.*?)\n---\n', content, re.DOTALL)
    return m.group(1) if m else ""


def _extract_title(content: str) -> str:
    m = re.search(r'^#\s+(.+)$', content, re.M)
    return m.group(1).strip() if m else ""


def plan_write(subdir, filename, content, action, exists_fn, read_fn):
    """日付プレフィックスの entity ファイルを entity-keyed + 時系列ログに正規化。

    Args:
        subdir: 親ディレクトリ名 (e.g. 'decisions')
        filename: LLM 出力のファイル名 (e.g. '2026-06-08-ff-cvr-weekly-meeting.md')
        content: 書き込む内容 (frontmatter + # title + 本文)
        action: 元の action ('create'/'append'/'replace')
        exists_fn(target_filename)->bool, read_fn(target_filename)->str: 既存 entity ページの I/O

    Returns: (target_filename, content_to_write, consolidated)
      consolidated=True なら caller は target に content_to_write を **上書き**するだけでよい。
      consolidated=False なら従来どおり (filename, content, action) で処理(passthrough)。
    """
    if subdir not in CONSOLIDATE_DIRS or not filename.endswith(".md"):
        return filename, content, False
    m = DATED_STEM.match(filename[:-3])
    if not m:
        return filename, content, False
    d, slug = m.group(1), m.group(2)
    target = f"{slug}.md"
    section_body = _strip_fm_and_title(content) or "(記録なし)"

    if exists_fn(target):
        existing = read_fn(target)
        if re.search(rf'(?m)^###\s+{re.escape(d)}\s*$', existing):
            return target, existing, True                 # idempotent: 同日分は既にある→無変更
        section = f"\n### {d}\n{section_body}\n"
        existing = re.sub(r'(?m)^updated:.*$', f'updated: {d}', existing, count=1)
        idx = existing.rfind("\n## 関連")
        new = (existing[:idx] + section + existing[idx:]) if idx >= 0 else existing.rstrip() + "\n" + section
        return target, new, True

    fm = _extract_fm(content).strip()
    if not fm:
        fm = f"updated: {d}\nconfidence: medium\ntags: []\nsources: []"
    elif "updated:" not in fm:
        fm = f"updated: {d}\n" + fm
    else:
        fm = re.sub(r'(?m)^updated:.*$', f'updated: {d}', fm, count=1)
    title = _extract_title(content) or slug
    page = f"---\n{fm}\n---\n# {title}\n\n## 時系列ログ\n\n### {d}\n{section_body}\n"
    return target, page, True
