"""
brain_wiki_helpers/frontmatter.py — Frontmatter / H2 セクションのマージ・分割 (★2026-05-22 Phase 3b)

brain_wiki.BrainWiki の以下 3 method を pure function 化:
- _merge_frontmatters → merge_frontmatters
- _split_h2_with_intro → split_h2_with_intro
- _normalize_heading → normalize_heading

dedup_all / _dedup_wiki_file から呼ばれる。self には依存していなかった。
"""
from __future__ import annotations

import re
from collections import OrderedDict
from typing import Tuple


def yaml_safe_scalar(s) -> str:
    """frontmatter のスカラ値として安全な double-quoted 文字列を返す。

    会議 title 等の外部由来文字列が改行や `---` / `key: value` で frontmatter 構造を
    壊したり別キー (例: clone_visibility: public) を注入するのを防ぐ。改行を空白化し、
    YAML double-quote 規則で \\ と " を escape して "..." で囲む。
    ★2026-06-10: compile_meeting_note の YAML injection 塞ぎ。
    """
    s = str(s).replace("\r", " ").replace("\n", " ").strip()
    s = s.replace("\\", "\\\\").replace('"', '\\"')
    return f'"{s}"'


def normalize_heading(s: str) -> str:
    """見出しテキストを正規化 (大文字小文字・空白・記号を無視)。"""
    s = s.strip().lower()
    s = re.sub(r"[\s　]+", " ", s)  # 連続空白を1つに
    s = re.sub(r"[^\w\sぁ-んァ-ヴ一-龥ー]", "", s)  # 記号除去
    return s.strip()


def merge_frontmatters(frontmatters: list[str]) -> str:
    """複数の YAML-like frontmatter を1つにマージ。

    ルール:
      - updated: 最新日付を残す
      - confidence: high > medium > low
      - tags / sources: union (順序維持)
      - その他: 最後に現れた値
    """
    rank = {"high": 3, "medium": 2, "low": 1}
    merged: "OrderedDict[str, object]" = OrderedDict()

    for fm in frontmatters:
        for line in fm.split("\n"):
            if ":" not in line:
                continue
            k, v = line.split(":", 1)
            k = k.strip()
            v = v.strip()
            if not k:
                continue

            if k in ("tags", "sources"):
                existing: list = merged.get(k, [])  # type: ignore[assignment]
                if not isinstance(existing, list):
                    existing = [str(existing)]
                items = [
                    i.strip()
                    for i in v.strip("[]").split(",")
                    if i.strip()
                ]
                for it in items:
                    if it not in existing:
                        existing.append(it)
                merged[k] = existing
            elif k == "updated":
                cur = merged.get(k, "")
                if v and str(v) > str(cur):
                    merged[k] = v
            elif k == "confidence":
                cur = merged.get(k, "")
                if rank.get(v, 0) >= rank.get(str(cur), 0):
                    merged[k] = v
            else:
                merged[k] = v

    lines = []
    for k, v in merged.items():
        if isinstance(v, list):
            lines.append(f"{k}: [{', '.join(v)}]")
        else:
            lines.append(f"{k}: {v}")
    return "\n".join(lines)


def split_h2_with_intro(text: str) -> Tuple[str, "OrderedDict[str, tuple[str, str]]"]:
    """H2 ヘッダで分割しつつ dedup。

    Returns:
        (intro_text, OrderedDict[normalized_heading -> (heading_line, body_text)])
    """
    h2_re = re.compile(r"^## ", re.MULTILINE)
    h2_starts = [m.start() for m in h2_re.finditer(text)]
    if not h2_starts:
        return text, OrderedDict()

    intro = text[:h2_starts[0]]
    sections: "OrderedDict[str, tuple[str, str]]" = OrderedDict()
    for i, s in enumerate(h2_starts):
        e = h2_starts[i + 1] if i + 1 < len(h2_starts) else len(text)
        block = text[s:e]
        lines = block.split("\n", 1)
        h2_line = lines[0].strip()
        body = lines[1] if len(lines) > 1 else ""
        key = normalize_heading(h2_line[3:])

        if key in sections:
            _, existing_body = sections[key]
            # 完全一致でなく、かつ新しい方が十分に長ければ差し替え
            if body.strip() != existing_body.strip():
                if len(body.strip()) > len(existing_body.strip()) * 1.2:
                    sections[key] = (h2_line, body)
                # それ以外は先勝ち (= 何もしない)
        else:
            sections[key] = (h2_line, body)

    return intro, sections
