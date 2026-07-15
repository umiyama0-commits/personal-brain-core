#!/usr/bin/env python3
"""
ingest_alignment_answers.py — 嗜好アラインメント回答 JSON を wiki/hobbies/ に反映する。

入力: data/brain/alignment/hobbies_alignment_2026-05-18.json (海山本人記入)
出力: data/brain/wiki/hobbies/{genre}/{slug}.md を以下の通り更新
  - frontmatter: importance: N + last_updated bump
  - "## 6. 📋 アラインメント質問" の "### 記入状況" を埋める
  - 各 Q (q1-q7) の答えを human-readable label に展開
  - q2-q5: STANDARD_QUESTIONS (build_alignment_form.py と同じ)
  - q1/q6/q7: wiki 本文の options から抽出

冪等: 既存の "### 記入状況" は丸ごと書き換え。
"""
import json
import re
import sys
from pathlib import Path
from datetime import datetime, timezone

ROOT = Path("/Users/umiyamatakeshi/brain-agent")
WIKI_DIR = ROOT / "data" / "brain" / "wiki" / "hobbies"
JSON_PATH = Path(sys.argv[1]) if len(sys.argv) > 1 else Path(
    "/Users/umiyamatakeshi/Downloads/umiyama_hobbies_alignment_2026-05-18-3.json"
)
TODAY = "2026-05-18"

STANDARD_OPTIONS = {
    "q2": {
        "label": "出会った時期",
        "a": "小〜中学生 (〜15 歳)",
        "b": "高校生 (16-18 歳)",
        "c": "大学生〜社会人初期 (19-25 歳)",
        "d": "起業準備期 (26-30 歳)",
        "e": "OWNDAYS 経営後 (2008-)",
        "f": "直近 5 年以内",
        "g": "覚えていない / 不明",
    },
    "q3": {
        "label": "作品との関わり方",
        "a": "何度も繰り返し読む / 観る / 聴く (定期的にリピート)",
        "b": "印象的だが、頻繁な再体験はしない",
        "c": "1 度だけだが深く刻まれた (再体験はしない)",
        "d": "久しぶりに再体験したい (今、戻りたい気分)",
        "e": "友人・部下に推薦した経験あり",
        "f": "コレクション所有 (本 / Blu-ray / アルバム / グッズ)",
        "g": "ふとした時に思い出して引用する",
    },
    "q4": {
        "label": "OWNDAYS 経営や人生節目での応用",
        "a": "経営判断 / 意思決定で具体的に引用したことがある",
        "b": "社員 / 部下 / 友人に「これ読んで / 観て / 聴いて」と推薦した",
        "c": "人生節目 (起業・転機・困難時) で精神的支柱になった",
        "d": "メタファー / 比喩として使った",
        "e": "ブランド / 表現 / VMV 等に取り入れた / 引用した",
        "f": "個人的な楽しみ / 価値観の確認用 (実務応用はしていない)",
        "g": "他の人と話す時の「共通言語」として使う",
    },
    "q5": {
        "label": "海山価値観の軸",
        "a": "A. 青春の終わりと出発",
        "b": "B. 凡人主人公の覚醒 / 努力と才能の非対称",
        "c": "C. 血縁ではない家族・居場所",
        "d": "D. 経営と人類史の交差",
        "e": "E. 辺境への愛着・場所性",
        "f": "F. 美しさ × 内面の正しさ",
        "g": "G. 家族最優先 / 時間 > お金",
        "h": "H. Take Bold Risks / Voice & Act / 自分たちの道を行く",
        "i": "その他 (コメント参照)",
    },
}

IMPORTANCE_LABEL = {
    5: "★5 中核作品",
    4: "★4 重要作品",
    3: "★3 標準",
    2: "★2 周辺",
    1: "★1 参考",
    0: "★0 (重み付け対象外)",
}


def parse_frontmatter(content: str):
    if not content.startswith("---\n"):
        return None, content
    end = content.find("\n---", 4)
    if end == -1:
        return None, content
    fm_text = content[4:end]
    body = content[end + 4:]
    if body.startswith("\n"):
        body = body[1:]
    return fm_text, body


def extract_q_options(body: str, q_key: str):
    """Extract options of Q1/Q6/Q7 from wiki body. Returns {a: label, b: label, ...}."""
    qnum = q_key.lstrip("q")
    # Match section starting with ### Q{n}. and ending at next ### or ## heading or EOF
    # Use re.DOTALL/re.MULTILINE for robust multiline matching.
    if qnum == "1":
        head_pat = r"###\s*Q1?\.\s*([^\n]+)"
    else:
        head_pat = rf"###\s*Q{qnum}\.\s*([^\n]+)"
    head_m = re.search(head_pat, body)
    if not head_m:
        return None, {}
    question = head_m.group(1).strip()
    rest = body[head_m.end():]
    # Find next ### or ## heading position
    next_head = re.search(r"\n(?:###|##)\s+", rest)
    block = rest[: next_head.start()] if next_head else rest
    opts = {}
    for opt_m in re.finditer(r"-\s*\[\s*\]\s*\(([a-z])\)\s*(.+)", block):
        opts[opt_m.group(1)] = opt_m.group(2).strip()
    return question, opts


def render_answer_lines(answer_codes, options):
    """Render answer codes as bullet list of selected labels."""
    if not answer_codes:
        return "(未選択)"
    lines = []
    for code in answer_codes:
        label = options.get(code, f"({code} = 不明)")
        lines.append(f"  - ({code}) {label}")
    return "\n".join(lines)


def render_standard_answer(q_key, answer_codes):
    opts = STANDARD_OPTIONS[q_key]
    if not answer_codes:
        return "(未選択)"
    lines = []
    for code in answer_codes:
        label = opts.get(code, f"({code} = 不明)")
        lines.append(f"  - ({code}) {label}")
    return "\n".join(lines)


def update_frontmatter(fm_text: str, importance: int, last_updated_iso: str):
    """Update or add `importance:` and update `last_updated:` in YAML-like frontmatter (simple line-based)."""
    lines = fm_text.split("\n")
    seen_importance = False
    seen_updated = False
    out = []
    for line in lines:
        if re.match(r"^\s*importance\s*:", line):
            out.append(f"importance: {importance}")
            seen_importance = True
        elif re.match(r"^\s*last_updated\s*:", line):
            out.append(f"last_updated: {last_updated_iso}")
            seen_updated = True
        else:
            out.append(line)
    if not seen_importance:
        # Insert importance after first_logged or after status, or at end
        new_out = []
        inserted = False
        for line in out:
            new_out.append(line)
            if not inserted and re.match(r"^\s*(first_logged|status)\s*:", line):
                new_out.append(f"importance: {importance}")
                inserted = True
        if not inserted:
            new_out.append(f"importance: {importance}")
        out = new_out
    if not seen_updated:
        out.append(f"last_updated: {last_updated_iso}")
    return "\n".join(out)


def build_answer_block(answer, q1q6q7_options):
    """Build the new 記入状況 block from the answer record."""
    importance = answer.get("importance", 0)
    importance_label = IMPORTANCE_LABEL.get(importance, f"★{importance}")
    last_updated_raw = answer.get("last_updated", "")
    last_updated_short = last_updated_raw[:10] if last_updated_raw else TODAY

    lines = []
    lines.append("### 記入状況")
    lines.append(f"- **回答日**: {last_updated_short}")
    lines.append(f"- **重要度 (importance)**: {importance} — {importance_label}")
    lines.append(f"- **last_updated**: {last_updated_short}")
    lines.append("")
    lines.append("### Q1 回答 (作品で最も響くもの)")
    q1_opts = q1q6q7_options.get("q1", {})
    lines.append(render_answer_lines(answer.get("q1", []), q1_opts))
    lines.append("")
    lines.append("### Q2 回答 (出会った時期)")
    lines.append(render_standard_answer("q2", answer.get("q2", [])))
    lines.append("")
    lines.append("### Q3 回答 (作品との関わり方)")
    lines.append(render_standard_answer("q3", answer.get("q3", [])))
    lines.append("")
    lines.append("### Q4 回答 (OWNDAYS 経営や人生節目での応用)")
    lines.append(render_standard_answer("q4", answer.get("q4", [])))
    lines.append("")
    lines.append("### Q5 回答 (海山価値観の軸)")
    lines.append(render_standard_answer("q5", answer.get("q5", [])))
    lines.append("")
    lines.append("### Q6 回答 (焼き付いて離れないシーン・セリフ・人物)")
    q6_opts = q1q6q7_options.get("q6", {})
    lines.append(render_answer_lines(answer.get("q6", []), q6_opts))
    lines.append("")
    lines.append("### Q7 回答 (経営観・人生観に植え付けたもの)")
    q7_opts = q1q6q7_options.get("q7", {})
    lines.append(render_answer_lines(answer.get("q7", []), q7_opts))
    lines.append("")
    lines.append("### コメント (海山本人記入)")
    comment = (answer.get("comment") or "").strip()
    if comment:
        lines.append(f"> {comment}")
    else:
        lines.append("> (コメント未記入)")
    return "\n".join(lines)


def replace_kinyu_status(body: str, new_block: str):
    """Replace the existing '### 記入状況' through next '## ' with new_block, preserving following content."""
    # Find '### 記入状況' position
    kinyu_re = re.compile(r"###\s*記入状況[\s\S]*?(?=\n---\n|\n##\s|\Z)")
    m = kinyu_re.search(body)
    if m:
        return body[:m.start()] + new_block + "\n" + body[m.end():]
    # No 記入状況 section — append before 7. 📌 or at end
    # Try inserting before "## 7." (深掘り backlog) or "## 関連" if exists
    insert_re = re.compile(r"(\n##\s+\d+\.\s+📌|\n##\s+\d+\.\s+関連)")
    m2 = insert_re.search(body)
    if m2:
        return body[:m2.start()] + "\n\n" + new_block + "\n" + body[m2.start():]
    return body.rstrip() + "\n\n" + new_block + "\n"


def main():
    with JSON_PATH.open() as f:
        data = json.load(f)
    answers = data["answers"]
    print(f"Loaded {len(answers)} answers from {JSON_PATH.name}")

    updated = 0
    missing = []
    no_alignment_section = []

    for work_id, answer in answers.items():
        # work_id like "manga/3-gatsu-no-lion"
        try:
            genre, slug = work_id.split("/", 1)
        except ValueError:
            print(f"  SKIP bad id: {work_id}")
            continue
        wiki_path = WIKI_DIR / genre / f"{slug}.md"
        if not wiki_path.exists():
            missing.append(work_id)
            continue
        content = wiki_path.read_text(encoding="utf-8")
        fm_text, body = parse_frontmatter(content)
        if fm_text is None:
            print(f"  SKIP no frontmatter: {wiki_path}")
            continue

        # Extract Q1/Q6/Q7 options from body
        q1q6q7 = {}
        for qk in ("q1", "q6", "q7"):
            _, opts = extract_q_options(body, qk)
            if opts:
                q1q6q7[qk] = opts

        if not q1q6q7.get("q1"):
            no_alignment_section.append(work_id)

        importance = answer.get("importance", 0)
        last_updated_raw = answer.get("last_updated", "")
        last_updated_short = last_updated_raw[:10] if last_updated_raw else TODAY

        new_fm = update_frontmatter(fm_text, importance, last_updated_short)
        new_block = build_answer_block(answer, q1q6q7)
        new_body = replace_kinyu_status(body, new_block)

        new_content = f"---\n{new_fm}\n---\n\n{new_body.lstrip()}"
        wiki_path.write_text(new_content, encoding="utf-8")
        updated += 1

    print(f"\nUpdated: {updated}")
    if missing:
        print(f"\nMissing wiki files ({len(missing)}):")
        for m in missing[:20]:
            print(f"  - {m}")
        if len(missing) > 20:
            print(f"  ... and {len(missing) - 20} more")
    if no_alignment_section:
        print(f"\nWiki without Q1 section ({len(no_alignment_section)}):")
        for w in no_alignment_section[:10]:
            print(f"  - {w}")


if __name__ == "__main__":
    main()
