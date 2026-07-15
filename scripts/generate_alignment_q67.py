#!/usr/bin/env python3
"""
generate_alignment_q67.py — 各 hobbies wiki に作品固有の Q6 / Q7 を LLM 生成して挿入する。

入力: data/brain/wiki/hobbies/{manga,books,movies,music}/*.md
出力: 同じ wiki ファイルに `### Q6.` / `### Q7.` セクションを Q1 の直後に挿入

設計:
- Anthropic API 直叩き (dev 環境では LiteLLM proxy が立っていないため)
- Claude Opus 4.7 (smart) を使用
- 並列実行 (ThreadPoolExecutor, max_workers=8)
- JSON 出力で {q6: {question, options[]}, q7: {question, options[]}}
- 既存 Q6/Q7 がある wiki はスキップ (idempotent)
- --force で上書き、--dry-run で実行しない、--limit N でテスト

実行:
    python3 scripts/generate_alignment_q67.py --dry-run --limit 1
    python3 scripts/generate_alignment_q67.py --limit 3  # 3 件だけ流す (検証)
    python3 scripts/generate_alignment_q67.py            # 全 157 件
"""
import argparse
import json
import os
import re
import sys
import time
import urllib.request
import urllib.error
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
WIKI_DIR = ROOT / "data" / "brain" / "wiki" / "hobbies"
ENV_FILE = ROOT / ".env"


def load_env():
    """Read .env minimally for ANTHROPIC_API_KEY."""
    env = {}
    if ENV_FILE.exists():
        for line in ENV_FILE.read_text().splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            k, _, v = line.partition("=")
            v = v.strip().strip('"').strip("'")
            env[k.strip()] = v
    return env


def claude_call(api_key: str, prompt: str, max_tokens: int = 1500, retries: int = 3):
    """Direct Anthropic API call with retry."""
    url = "https://api.anthropic.com/v1/messages"
    payload = {
        "model": "claude-opus-4-20250514",  # Opus 4 (4.7 alias 解決失敗時の fallback として確実なバージョン指定)
        "max_tokens": max_tokens,
        "messages": [{"role": "user", "content": prompt}],
    }
    body = json.dumps(payload).encode("utf-8")
    last_err = None
    for attempt in range(retries):
        try:
            req = urllib.request.Request(
                url,
                data=body,
                headers={
                    "x-api-key": api_key,
                    "anthropic-version": "2023-06-01",
                    "content-type": "application/json",
                },
                method="POST",
            )
            with urllib.request.urlopen(req, timeout=60) as resp:
                data = json.loads(resp.read().decode("utf-8"))
                # response: {"content": [{"type": "text", "text": "..."}], ...}
                for block in data.get("content", []):
                    if block.get("type") == "text":
                        return block["text"]
                return ""
        except urllib.error.HTTPError as e:
            last_err = f"HTTP {e.code}: {e.read().decode('utf-8', errors='ignore')[:300]}"
            if e.code in (429, 500, 502, 503, 504):
                time.sleep(2 ** attempt + (attempt * 0.5))
                continue
            raise RuntimeError(last_err)
        except Exception as e:
            last_err = str(e)
            time.sleep(2 ** attempt)
    raise RuntimeError(f"All retries failed: {last_err}")


PROMPT_TEMPLATE = """あなたは海山丈司 (OWNDAYS CEO) の Personal Brain AI を構築している。
以下は海山さんが好きな{genre_jp}作品「{title}」({author_label}) の Wiki エントリの本文。

---WIKI BODY START---
{wiki_body}
---WIKI BODY END---

このエントリを基に、海山さん本人記入用の「作品固有・深掘り 2 問」(Q6 と Q7) を作成して。

### Q6: 印象に強く残るもの (深掘り 1/2)
- この作品の中で**最も強く残るもの**は何か (シーン / セリフ / キャラクター / 構造 / 雰囲気)
- 質問文は**この作品の固有名詞 / 文脈を入れて 30-60 字で自然に**phrase する
  - 例 (3月のライオン): 「『3月のライオン』で焼き付いて離れない場面・セリフ・キャラは?」
  - 例 (BECK):「BECK で最も鳴り続けているシーンや音は?」
- 選択肢は**作品固有の具体的アイテムを 5-7 個** (a)-(g)
  - 必ずその作品の登場人物名・固有シーン・固有セリフ・固有曲・固有場所を入れる
  - 例 (3月のライオン): 「(a) 川本家の食卓シーン」「(b) 林田先生『でも100個揃えば...』」「(c) ひなたの 'こんな所...生きて卒業さえすれば 私の勝ちだ'」「(d) 二海堂の 『潔いと投げやりは違う』」
  - 例 (BECK): 「(a) コユキがマイクの前で声が出ない瞬間」「(b) 千葉の "本物は本物だ"」「(c) ラスト 'Slip Out' のステージ」
  - 最後の選択肢は **「(X) その他 (コメントに記入)」** で締める ((X) は最後のキー: g or h)

### Q7: 自分に残したもの (深掘り 2/2)
- この作品が**海山さん本人の仕事 / 価値観 / 人生 / 生き方**に残したものは何か
- 質問文は**作品名と海山さん固有の文脈を絡めて 30-60 字**で phrase
  - 例 (3月のライオン): 「『3月のライオン』が海山さんの経営観・人生観に植えたものは?」
  - 例 (サピエンス全史): 「サピエンス全史が海山さんの意思決定の地軸に残したものは?」
- 選択肢は**この作品ならではの "教訓 / 視点 / 態度"** を 5-7 個 (a)-(g)
  - 抽象的すぎず、作品の中身が透けて見える選択肢に
  - 例 (3月のライオン): 「(a) 血縁ではない家族・居場所への確信」「(b) 才能差を継続で埋める島田八段的経営観」「(c) 早く人生を決めたことへの肯定」「(d) 後悔より『逃げなかった記憶』への執着」
  - 例 (BECK): 「(a) 凡人が天才に並ぶ瞬間への信奉」「(b) 仲間と作る音への憧憬」「(c) スポットライト前の恐れを呑む覚悟」
  - 最後の選択肢は **「(X) その他 (コメントに記入)」**

### 出力フォーマット (厳格に JSON のみ、前置き禁止):
```json
{{
  "q6": {{
    "question": "Q6 質問文 (30-60字)",
    "options": [
      {{"key": "a", "label": "..."}},
      {{"key": "b", "label": "..."}},
      ...
      {{"key": "g", "label": "その他 (コメントに記入)"}}
    ]
  }},
  "q7": {{
    "question": "Q7 質問文 (30-60字)",
    "options": [...]
  }}
}}
```

注意:
- 質問文と選択肢は**この作品でしか成立しない**具体度で。「印象的なシーン」「人生への影響」のような汎用回答は避ける
- 選択肢は 6-8 個 (= a-g or a-h)、最後は「その他 (コメントに記入)」
- options[].label は 50 字以内、固有名詞は引用符で囲ってOK
- 出力は JSON だけ、説明文・コードブロックフェンス以外の前後文は禁止
"""


def parse_frontmatter(content: str):
    if not content.startswith("---"):
        return {}, content
    end = content.find("\n---", 3)
    if end == -1:
        return {}, content
    fm_text = content[3:end].strip()
    body = content[end + 4:]
    fm = {}
    for line in fm_text.split("\n"):
        m = re.match(r"^([a-zA-Z_][a-zA-Z0-9_]*)\s*:\s*(.*)$", line.rstrip())
        if m:
            v = re.sub(r"\s+#.*$", "", m.group(2).strip())
            if (v.startswith('"') and v.endswith('"')) or (v.startswith("'") and v.endswith("'")):
                v = v[1:-1]
            fm[m.group(1)] = v
    return fm, body


GENRE_JP = {"manga": "漫画", "books": "本", "movies": "映画", "music": "音楽"}


def extract_section(text: str) -> str:
    """Trim wiki body for prompt — drop very long quote lists."""
    # 大体 4000 字に圧縮 (long wiki でも prompt 詰まらない)
    if len(text) <= 4500:
        return text
    return text[:4500] + "\n... (省略)"


def already_has_q67(body: str) -> bool:
    """Check if wiki already has Q6 + Q7 sections under 📋 section."""
    m = re.search(r"##\s*\d+\.\s*📋\s*アラインメント質問[^\n]*\n", body)
    if not m:
        return False
    rest = body[m.end():]
    next_section = re.search(r"\n##\s+\d+\.\s+", rest)
    section = rest[: next_section.start()] if next_section else rest
    return ("### Q6." in section) and ("### Q7." in section)


def format_q_md(q_key: str, question: str, options: list) -> str:
    """Render Q6/Q7 section as markdown."""
    lines = [f"### {q_key}. {question}"]
    for opt in options:
        lines.append(f"- [ ] ({opt['key']}) {opt['label']}")
    return "\n".join(lines)


def insert_q67_into_wiki(content: str, q6_md: str, q7_md: str) -> str:
    """Insert Q6+Q7 sections after Q1 (and before コメント) inside 📋 section."""
    # Find 📋 section
    m = re.search(r"(##\s*\d+\.\s*📋\s*アラインメント質問[^\n]*\n)", content)
    if not m:
        raise ValueError("📋 アラインメント質問 section not found")
    section_start = m.end()
    # Section ends at next ## or end
    rest = content[section_start:]
    next_section = re.search(r"\n##\s+\d+\.\s+", rest)
    section_end_idx = section_start + next_section.start() if next_section else len(content)
    section_body = content[section_start:section_end_idx]

    # 既存 Q1 ブロック (### Q. or ### Q1.) を「### Q1.」に renameし、その直後に Q6/Q7 挿入
    # 既存形式は "### Q. ..." または "### Q1. ..."
    new_section = section_body

    # 1. Q1 リネーム: "### Q. " → "### Q1. " (まだリネームされていない場合)
    new_section = re.sub(r"(?m)^###\s*Q\.\s+", "### Q1. ", new_section, count=1)

    # 2. Q1 ブロック の終わり (= 次の ### か または empty line + ### コメント) を見つけて、その前に Q6/Q7 挿入
    # コメント (自由記入) または 記入状況 セクションの前に挿入
    insert_marker = re.search(r"\n###\s*コメント[^\n]*", new_section)
    if not insert_marker:
        insert_marker = re.search(r"\n###\s*記入状況[^\n]*", new_section)
    if insert_marker:
        insert_pos = insert_marker.start()
        new_section = (
            new_section[:insert_pos]
            + "\n\n"
            + q6_md
            + "\n\n"
            + q7_md
            + "\n"
            + new_section[insert_pos:]
        )
    else:
        # コメントセクションもない場合は末尾に追加
        new_section = new_section.rstrip() + "\n\n" + q6_md + "\n\n" + q7_md + "\n"

    return content[:section_start] + new_section + content[section_end_idx:]


def process_one(wiki_file: Path, api_key: str, force: bool, dry_run: bool):
    """Process a single wiki file."""
    content = wiki_file.read_text(encoding="utf-8")
    fm, body = parse_frontmatter(content)

    if already_has_q67(body) and not force:
        return ("skip", wiki_file, "already has Q6+Q7")

    title = fm.get("title", wiki_file.stem)
    author = fm.get("author") or fm.get("artist", "")
    genre = wiki_file.parent.name
    genre_jp = GENRE_JP.get(genre, genre)
    author_label = author if author else "(著者/アーティスト不明)"

    # Trim body for prompt
    body_trimmed = extract_section(body)

    prompt = PROMPT_TEMPLATE.format(
        title=title,
        author_label=author_label,
        genre_jp=genre_jp,
        wiki_body=body_trimmed,
    )

    if dry_run:
        return ("dryrun", wiki_file, f"would call LLM for {title}")

    try:
        raw = claude_call(api_key, prompt, max_tokens=1800)
    except Exception as e:
        return ("error", wiki_file, f"LLM call failed: {e}")

    # Parse JSON from response (strip potential code fence)
    json_text = raw.strip()
    json_text = re.sub(r"^```(?:json)?\s*", "", json_text)
    json_text = re.sub(r"\s*```$", "", json_text)

    try:
        data = json.loads(json_text)
    except json.JSONDecodeError as e:
        # Try to extract first {...} block
        m = re.search(r"\{[\s\S]*\}", json_text)
        if m:
            try:
                data = json.loads(m.group(0))
            except Exception:
                return ("error", wiki_file, f"JSON parse failed: {e}; raw[:200]={raw[:200]}")
        else:
            return ("error", wiki_file, f"JSON parse failed: {e}; raw[:200]={raw[:200]}")

    if "q6" not in data or "q7" not in data:
        return ("error", wiki_file, f"missing q6 or q7 keys: {list(data.keys())}")

    q6 = data["q6"]
    q7 = data["q7"]
    if "question" not in q6 or "options" not in q6 or "question" not in q7 or "options" not in q7:
        return ("error", wiki_file, "missing question/options in q6 or q7")

    q6_md = format_q_md("Q6", q6["question"], q6["options"])
    q7_md = format_q_md("Q7", q7["question"], q7["options"])

    try:
        new_content = insert_q67_into_wiki(content, q6_md, q7_md)
    except Exception as e:
        return ("error", wiki_file, f"insert failed: {e}")

    wiki_file.write_text(new_content, encoding="utf-8")
    return ("ok", wiki_file, f"inserted Q6/Q7 for {title}")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--force", action="store_true", help="Overwrite existing Q6/Q7")
    ap.add_argument("--dry-run", action="store_true", help="Show actions without API call / file write")
    ap.add_argument("--limit", type=int, default=0, help="Process only first N files (test)")
    ap.add_argument("--workers", type=int, default=6, help="Parallel workers")
    ap.add_argument("--genres", type=str, default="manga,books,movies,music",
                    help="Comma-separated genre dirs")
    ap.add_argument("--only", type=str, default="",
                    help="Comma-separated wiki slugs to process (e.g. '3-gatsu-no-lion,beck')")
    args = ap.parse_args()

    env = load_env()
    api_key = env.get("ANTHROPIC_API_KEY")
    if not api_key:
        print("ERROR: ANTHROPIC_API_KEY not found in .env", file=sys.stderr)
        sys.exit(1)

    # Collect files
    genres = [g.strip() for g in args.genres.split(",") if g.strip()]
    only_set = {s.strip() for s in args.only.split(",") if s.strip()}
    files = []
    for genre in genres:
        gdir = WIKI_DIR / genre
        if not gdir.exists():
            continue
        for f in sorted(gdir.glob("*.md")):
            if only_set and f.stem not in only_set:
                continue
            files.append(f)

    if args.limit:
        files = files[: args.limit]

    print(f"Target: {len(files)} files (genres={genres}, force={args.force}, dry_run={args.dry_run}, workers={args.workers})")
    print("---")

    counts = {"ok": 0, "skip": 0, "error": 0, "dryrun": 0}
    started = time.time()

    if args.dry_run or args.workers == 1:
        results = []
        for f in files:
            r = process_one(f, api_key, args.force, args.dry_run)
            results.append(r)
            print(f"[{r[0].upper()}] {f.relative_to(ROOT)} — {r[2]}")
            counts[r[0]] += 1
    else:
        with ThreadPoolExecutor(max_workers=args.workers) as ex:
            futures = {ex.submit(process_one, f, api_key, args.force, args.dry_run): f for f in files}
            for fut in as_completed(futures):
                r = fut.result()
                f = futures[fut]
                print(f"[{r[0].upper()}] {f.relative_to(ROOT)} — {r[2]}")
                counts[r[0]] += 1

    elapsed = time.time() - started
    print("---")
    print(f"Done in {elapsed:.1f}s")
    print(f"  OK:     {counts['ok']}")
    print(f"  SKIP:   {counts['skip']}")
    print(f"  ERROR:  {counts['error']}")
    print(f"  DRYRUN: {counts['dryrun']}")

    if counts["error"] > 0:
        sys.exit(2)


if __name__ == "__main__":
    main()
