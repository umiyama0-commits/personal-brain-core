"""services/life_story.py — 年代記 (自伝の章) 取込 (★2026-07-05 海山指示「0-42歳の記録を人格補完に」)。

海山本人が書いた自伝テキスト (年代の章単位) を受け取り、二層で人格補完に流す:
  ① 原文そのまま → wiki/interview/chronicle.md (deep-private、無加工の永久保存 = /diary と同じ原文主義)
  ② 蒸留 → 既存の音声/フォームと同一パイプライン (chunk 分割・話者帰属・較正・レビュー・coverage 加点)

経路: POST /api/life-story/submit?token=VOICE_ALIGN_TOKEN {"title": "...", "text": "..."}
SSH 不要 (公開 API + auto_deploy) — Studio への直接アクセスが無くても章を積める。
"""
from __future__ import annotations

import re
from datetime import datetime

MAX_CHAPTER_CHARS = 40_000   # 蒸留 chunk 上限 (3×14k) に収まる範囲。超える章は分割送信。
MIN_CHAPTER_CHARS = 100


def sanitize_chapter(text: str) -> str:
    """wiki 直書き用の無害化 (diary 経路と同じ思想)。
    - 行頭 `---` は frontmatter 境界と誤認され /dedup merge で visibility 反転し得る → 置換
    - `clone_visibility:` の文字列は frontmatter への昇格を防ぐため無害化
    内容は変えない (原文主義)、危険なマークアップだけ骨抜きにする。"""
    out = []
    for ln in text.splitlines():
        if ln.strip() == "---":
            ln = "----"
        if "clone_visibility" in ln:
            ln = ln.replace("clone_visibility", "clone·visibility")
        out.append(ln)
    return "\n".join(out)


def chapter_header(title: str) -> str:
    safe = re.sub(r"[#\n\r]", "", (title or "").strip())[:80] or "無題の章"
    return f"# 年代記: {safe}"


def build_transcript(title: str, text: str) -> str:
    """蒸留パイプラインへ渡す transcript。全文が本人の言葉であることを明示
    (EXTRACT_PROMPT の話者帰属制約と整合)。"""
    return (
        f"{chapter_header(title)}\n"
        f"(以下は海山本人が書いた自伝テキスト。全文が海山の言葉。音声でなく文章による年代記)\n\n"
        f"海山: {text.strip()}\n"
    )


def chronicle_frontmatter() -> str:
    return (
        "---\n"
        f"updated: {datetime.now().strftime('%Y-%m-%d')}\n"
        "clone_visibility: private\n"
        "tags: [人格, 年代記, 自伝]\n"
        "description: 海山本人が書いた自伝の年代記 (原文そのまま・無加工)\n"
        "---\n\n"
        "# 海山丈司 年代記 (本人記述・原文)\n\n"
        "> 各章は本人がテキストで書いた原文。蒸留 (16次元への分類) は別途\n"
        "> interview_extracted のレビューを経て各次元 wiki に入る。ここは一次資料。\n"
    )


def validate_chapter(title: str, text: str) -> str | None:
    """エラーメッセージ or None (OK)。"""
    if not (title or "").strip():
        return "title が空"
    t = (text or "").strip()
    if len(t) < MIN_CHAPTER_CHARS:
        return f"text が短すぎる ({len(t)}字 < {MIN_CHAPTER_CHARS})"
    if len(t) > MAX_CHAPTER_CHARS:
        return f"text が長すぎる ({len(t)}字 > {MAX_CHAPTER_CHARS}) — 章を分割して送信を"
    return None
