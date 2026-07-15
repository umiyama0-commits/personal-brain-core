#!/usr/bin/env python3
"""
build_alignment_form.py — 嗜好データ wiki から アラインメント質問を抽出して
単一 HTML フォームを生成する。

入力: data/brain/wiki/hobbies/{manga,books,movies,music}/*.md
出力: data/brain/alignment/hobbies_alignment_form.html

特徴:
- 各 wiki の "## N. 📋 アラインメント質問" セクションをパース
- 単一 HTML (CSS / JS / データすべて inline) → 配布 / オープン用
- 回答は localStorage に自動保存
- JSON download (回答エクスポート) + JSON upload (再開)
- ジャンル絞り込み・キーワード検索・進捗バー
- 海山さん本人記入用、PC / モバイル両対応
"""
import json
import re
import sys
from pathlib import Path
from datetime import datetime

ROOT = Path(__file__).resolve().parent.parent
WIKI_DIR = ROOT / "data" / "brain" / "wiki" / "hobbies"
OUT_DIR = ROOT / "data" / "brain" / "alignment"
OUT_FILE = OUT_DIR / "hobbies_alignment_form.html"


def parse_frontmatter(content: str):
    """Extract YAML frontmatter (simple line-by-line parse, no PyYAML dependency)."""
    if not content.startswith("---"):
        return {}, content
    end = content.find("\n---", 3)
    if end == -1:
        return {}, content
    fm_text = content[3:end].strip()
    body = content[end + 4:]
    fm = {}
    for line in fm_text.split("\n"):
        line = line.rstrip()
        if not line or line.startswith("#"):
            continue
        m = re.match(r"^([a-zA-Z_][a-zA-Z0-9_]*)\s*:\s*(.*)$", line)
        if m:
            key = m.group(1)
            val = m.group(2).strip()
            # strip trailing comments
            val = re.sub(r"\s+#.*$", "", val)
            # quoted strings
            if (val.startswith('"') and val.endswith('"')) or (
                val.startswith("'") and val.endswith("'")
            ):
                val = val[1:-1]
            fm[key] = val
    return fm, body


def extract_alignment(body: str):
    """Extract all Q* (Q1/Q6/Q7) + options + comment prompt from '📋 アラインメント質問' section.

    ★ 2026-05-17: Q1 のみ → Q1/Q6/Q7 の複数 Q (作品固有) サポート。
    旧形式 "### Q." も自動的に q1 として扱う (後方互換)。
    """
    m = re.search(r"##\s*\d+\.\s*📋\s*アラインメント質問[^\n]*\n", body)
    if not m:
        return None
    section_start = m.end()
    rest = body[section_start:]

    end_match = re.search(r"\n##\s+\d+\.\s+", rest)
    section = rest[: end_match.start()] if end_match else rest

    # 全 ### Q<N>. (or 旧 ### Q.) 行の位置 + key + 質問文 を収集
    q_starts = []
    for m_q in re.finditer(r"(?m)^###\s*Q(\d*)\.\s*([^\n]+)$", section):
        key = m_q.group(1) or "1"  # 旧 "### Q." → q1
        q_text = re.sub(r"\*\*([^*]+)\*\*", r"\1", m_q.group(2).strip())
        q_starts.append({"pos": m_q.start(), "body_start": m_q.end(), "key": key, "text": q_text})

    if not q_starts:
        return None

    questions = {}
    for i, qs in enumerate(q_starts):
        # ブロック終端: 次の Q の開始位置 or コメント節 or section 末尾
        if i + 1 < len(q_starts):
            block_end = q_starts[i + 1]["pos"]
        else:
            comment_m = re.search(r"\n###\s*コメント", section[qs["body_start"]:])
            block_end = qs["body_start"] + comment_m.start() if comment_m else len(section)
        q_body = section[qs["body_start"]:block_end]

        opts = []
        for opt_m in re.finditer(r"-\s*\[\s*\]\s*\(([a-z])\)\s*(.+)", q_body):
            label = re.sub(r"\*\*([^*]+)\*\*", r"\1", opt_m.group(2).strip())
            opts.append({"key": opt_m.group(1), "label": label})

        questions[f"q{qs['key']}"] = {"question": qs["text"], "options": opts}

    # コメント案内文
    comment_block = re.search(
        r"###\s*コメント[^\n]*\n((?:>\s.+\n?)+)", section
    )
    comment_prompt = ""
    if comment_block:
        lines = []
        for line in comment_block.group(1).split("\n"):
            stripped = re.sub(r"^>\s?", "", line).strip()
            if stripped:
                lines.append(stripped)
        comment_prompt = " ".join(lines)

    if "q1" not in questions:
        return None

    return {
        "questions": questions,  # {q1: {question, options}, q6: ..., q7: ...}
        "comment_prompt": comment_prompt,
    }


def collect_works():
    works = []
    for genre in ["manga", "books", "movies", "music"]:
        genre_dir = WIKI_DIR / genre
        if not genre_dir.exists():
            continue
        for f in sorted(genre_dir.glob("*.md")):
            content = f.read_text(encoding="utf-8")
            fm, body = parse_frontmatter(content)
            align = extract_alignment(body)
            if not align:
                print(f"  WARN: no alignment section in {f.relative_to(ROOT)}", file=sys.stderr)
                continue
            works.append(
                {
                    "id": f"{genre}/{f.stem}",
                    "slug": f.stem,
                    "genre": genre,
                    "title": fm.get("title", f.stem),
                    "author": fm.get("author") or fm.get("artist", ""),
                    "year": str(fm.get("year", "")),
                    "publisher": fm.get("publisher", ""),
                    "tags": fm.get("tags", ""),
                    # ★ 2026-05-17: q1, q6, q7 を作品固有として wiki から取得
                    "questions": align["questions"],
                    "comment_prompt": align["comment_prompt"],
                }
            )
    return works


GENRE_LABELS = {
    "manga": "漫画",
    "books": "本",
    "movies": "映画",
    "music": "音楽",
}


# Q2-Q5 は全作品共通の標準質問 (横軸: 出会い時期 / 関わり方 / 経営応用 / 海山価値観の軸)
# Q1/Q6/Q7 は作品固有 (wiki ファイル側で管理、LLM 生成済み)
# ★ 2026-05-17 改訂:
#   - Q3/Q4 にラベル微調整 + (g) 追加
#   - Q6/Q7 を STANDARD から外し、作品固有 (wiki) に移動
# 既存 v2 JSON (q1-q5 のみ) は q6/q7 を空配列で補完して v3 に migration (UI 側で処理)
STANDARD_QUESTIONS = [
    {
        "key": "q2",
        "label": "出会った時期は?",
        "options": [
            {"key": "a", "label": "小〜中学生 (〜15 歳)"},
            {"key": "b", "label": "高校生 (16-18 歳)"},
            {"key": "c", "label": "大学生〜社会人初期 (19-25 歳)"},
            {"key": "d", "label": "起業準備期 (26-30 歳)"},
            {"key": "e", "label": "OWNDAYS 経営後 (2008-)"},
            {"key": "f", "label": "直近 5 年以内"},
            {"key": "g", "label": "覚えていない / 不明"},
        ],
    },
    {
        "key": "q3",
        "label": "この作品との関わり方は? (複数可)",
        "options": [
            {"key": "a", "label": "何度も繰り返し読む / 観る / 聴く (定期的にリピート)"},
            {"key": "b", "label": "印象的だが、頻繁な再体験はしない"},
            {"key": "c", "label": "1 度だけだが深く刻まれた (再体験はしない)"},
            {"key": "d", "label": "久しぶりに再体験したい (今、戻りたい気分)"},
            {"key": "e", "label": "友人・部下に推薦した経験あり"},
            {"key": "f", "label": "コレクション所有 (本 / Blu-ray / アルバム / グッズ)"},
            {"key": "g", "label": "ふとした時に思い出して引用する"},
        ],
    },
    {
        "key": "q4",
        "label": "OWNDAYS 経営や人生節目での応用は? (複数可)",
        "options": [
            {"key": "a", "label": "経営判断 / 意思決定で具体的に引用したことがある"},
            {"key": "b", "label": "社員 / 部下 / 友人に「これ読んで / 観て / 聴いて」と推薦した"},
            {"key": "c", "label": "人生節目 (起業・転機・困難時) で精神的支柱になった"},
            {"key": "d", "label": "メタファー / 比喩として使った"},
            {"key": "e", "label": "ブランド / 表現 / VMV 等に取り入れた / 引用した"},
            {"key": "f", "label": "個人的な楽しみ / 価値観の確認用 (実務応用はしていない)"},
            {"key": "g", "label": "他の人と話す時の「共通言語」として使う"},
        ],
    },
    {
        "key": "q5",
        "label": "海山価値観のどの軸と結びつく? (複数可)",
        "options": [
            {"key": "a", "label": "A. 青春の終わりと出発"},
            {"key": "b", "label": "B. 凡人主人公の覚醒 / 努力と才能の非対称"},
            {"key": "c", "label": "C. 血縁ではない家族・居場所"},
            {"key": "d", "label": "D. 経営と人類史の交差"},
            {"key": "e", "label": "E. 辺境への愛着・場所性"},
            {"key": "f", "label": "F. 美しさ × 内面の正しさ"},
            {"key": "g", "label": "G. 家族最優先 / 時間 > お金"},
            {"key": "h", "label": "H. Take Bold Risks / Voice & Act / 自分たちの道を行く"},
            {"key": "i", "label": "その他 (コメントに記入)"},
        ],
    },
    # ★ 2026-05-17: Q6 (印象に強く残るもの) / Q7 (自分に残したもの) は作品固有に移動
    # 各 wiki の "### Q6." / "### Q7." セクションから extract_alignment() が抽出する
]


HTML_TEMPLATE = """<!DOCTYPE html>
<html lang="ja">
<head>
<meta charset="UTF-8" />
<meta name="viewport" content="width=device-width, initial-scale=1.0" />
<title>海山丈司 — 嗜好アラインメント記入フォーム</title>
<style>
:root {
  --bg: #fafafa;
  --card-bg: #fff;
  --border: #e0e0e0;
  --text: #1a1a1a;
  --muted: #666;
  --primary: #2563eb;
  --primary-hover: #1d4ed8;
  --success: #10b981;
  --warn: #f59e0b;
  --tag-manga: #fef3c7;
  --tag-books: #dbeafe;
  --tag-movies: #fce7f3;
  --tag-music: #d1fae5;
}
* { box-sizing: border-box; }
body {
  font-family: -apple-system, BlinkMacSystemFont, "Hiragino Sans", "Yu Gothic", sans-serif;
  background: var(--bg);
  color: var(--text);
  margin: 0;
  padding: 0;
  line-height: 1.6;
}
header {
  position: sticky;
  top: 0;
  z-index: 100;
  background: var(--card-bg);
  border-bottom: 1px solid var(--border);
  padding: 12px 16px;
  box-shadow: 0 1px 3px rgba(0,0,0,0.04);
}
.header-inner {
  max-width: 920px;
  margin: 0 auto;
  display: flex;
  flex-direction: column;
  gap: 8px;
}
h1 {
  margin: 0;
  font-size: 18px;
  font-weight: 600;
}
.controls {
  display: flex;
  flex-wrap: wrap;
  gap: 8px;
  align-items: center;
}
.search {
  flex: 1;
  min-width: 180px;
  padding: 8px 12px;
  border: 1px solid var(--border);
  border-radius: 6px;
  font-size: 14px;
}
.filter-chip {
  padding: 6px 12px;
  border: 1px solid var(--border);
  background: #fff;
  border-radius: 16px;
  cursor: pointer;
  font-size: 13px;
  transition: all 0.15s;
}
.filter-chip.active {
  background: var(--primary);
  color: #fff;
  border-color: var(--primary);
}
.filter-chip:hover:not(.active) { background: #f5f5f5; }
.btn {
  padding: 8px 14px;
  border-radius: 6px;
  border: none;
  cursor: pointer;
  font-size: 14px;
  font-weight: 500;
  transition: all 0.15s;
}
.btn-primary {
  background: var(--primary);
  color: #fff;
}
.btn-primary:hover { background: var(--primary-hover); }
.btn-secondary {
  background: #fff;
  color: var(--text);
  border: 1px solid var(--border);
}
.btn-secondary:hover { background: #f5f5f5; }
.progress-bar {
  height: 4px;
  background: #e5e7eb;
  border-radius: 2px;
  overflow: hidden;
}
.progress-fill {
  height: 100%;
  background: var(--success);
  transition: width 0.3s;
}
.progress-text {
  font-size: 12px;
  color: var(--muted);
  text-align: right;
}
main {
  max-width: 920px;
  margin: 16px auto 80px;
  padding: 0 16px;
}
.work-card {
  background: var(--card-bg);
  border: 1px solid var(--border);
  border-radius: 8px;
  padding: 16px;
  margin-bottom: 12px;
  transition: opacity 0.2s;
  border-left: 4px solid transparent;
}
.work-card.hidden { display: none; }
.work-card.partial { border-left-color: var(--warn); }
.work-card.full { border-left-color: var(--success); }
.question-block {
  margin: 12px 0;
  padding: 8px 10px;
  border-left: 2px solid transparent;
  border-radius: 4px;
  transition: all 0.15s;
}
.question-block.answered {
  border-left-color: var(--success);
  background: rgba(16, 185, 129, 0.04);
}
.dots {
  display: flex;
  gap: 3px;
  margin-left: auto;
}
.dot {
  display: inline-block;
  font-size: 9px;
  font-weight: 600;
  padding: 2px 5px;
  border-radius: 8px;
  background: #e5e7eb;
  color: #9ca3af;
  letter-spacing: 0.5px;
}
.dot.filled {
  background: var(--success);
  color: #fff;
}
.importance-row {
  display: flex;
  align-items: center;
  gap: 8px;
  margin: 8px 0 4px;
  padding: 8px 10px;
  background: #fffbeb;
  border: 1px solid #fde68a;
  border-radius: 6px;
}
.importance-label {
  font-size: 12px;
  color: var(--muted);
  font-weight: 500;
}
.stars {
  display: flex;
  gap: 1px;
}
.star {
  cursor: pointer;
  font-size: 22px;
  color: #e5e7eb;
  transition: color 0.1s, transform 0.1s;
  user-select: none;
  line-height: 1;
}
.star.filled { color: #f59e0b; }
.star:hover { transform: scale(1.15); color: #fbbf24; }
.importance-value {
  font-size: 12px;
  color: var(--muted);
  margin-left: 4px;
  min-width: 50px;
}
.importance-clear {
  font-size: 11px;
  color: var(--muted);
  cursor: pointer;
  text-decoration: underline;
  margin-left: auto;
}
.importance-clear:hover { color: var(--text); }
.work-header {
  display: flex;
  flex-wrap: wrap;
  align-items: center;
  gap: 8px;
  margin-bottom: 8px;
}
.work-title {
  font-size: 16px;
  font-weight: 600;
  margin: 0;
}
.work-meta {
  font-size: 12px;
  color: var(--muted);
}
.genre-tag {
  display: inline-block;
  padding: 2px 8px;
  border-radius: 10px;
  font-size: 11px;
  font-weight: 500;
}
.genre-tag.manga { background: var(--tag-manga); color: #92400e; }
.genre-tag.books { background: var(--tag-books); color: #1e40af; }
.genre-tag.movies { background: var(--tag-movies); color: #be185d; }
.genre-tag.music { background: var(--tag-music); color: #065f46; }
.question {
  font-size: 14px;
  font-weight: 500;
  margin: 8px 0 4px;
  color: var(--text);
}
.options {
  display: flex;
  flex-direction: column;
  gap: 4px;
  margin: 6px 0;
}
.option {
  display: flex;
  align-items: flex-start;
  gap: 8px;
  padding: 6px 10px;
  border-radius: 6px;
  cursor: pointer;
  font-size: 13px;
  transition: background 0.1s;
}
.option:hover { background: #f5f5f5; }
.option input[type="checkbox"] {
  margin-top: 2px;
  cursor: pointer;
  flex-shrink: 0;
}
.option-key {
  font-weight: 600;
  color: var(--muted);
  flex-shrink: 0;
}
.comment-label {
  font-size: 12px;
  color: var(--muted);
  margin: 8px 0 4px;
}
.comment-prompt {
  font-size: 11px;
  color: var(--muted);
  font-style: italic;
  margin-bottom: 4px;
}
textarea {
  width: 100%;
  min-height: 64px;
  padding: 8px;
  border: 1px solid var(--border);
  border-radius: 6px;
  font-size: 13px;
  font-family: inherit;
  resize: vertical;
}
textarea:focus, .search:focus {
  outline: 2px solid var(--primary);
  border-color: transparent;
}
.save-indicator {
  font-size: 11px;
  color: var(--success);
  margin-top: 4px;
  opacity: 0;
  transition: opacity 0.3s;
}
.save-indicator.show { opacity: 1; }
.fab {
  position: fixed;
  bottom: 20px;
  right: 20px;
  display: flex;
  flex-direction: column;
  gap: 8px;
  z-index: 200;
}
.fab .btn {
  box-shadow: 0 2px 8px rgba(0,0,0,0.15);
}
.empty-state {
  text-align: center;
  padding: 40px 20px;
  color: var(--muted);
  font-size: 14px;
}
.genre-section {
  margin-top: 24px;
}
.genre-section-title {
  font-size: 16px;
  font-weight: 600;
  margin: 16px 0 8px;
  padding-bottom: 4px;
  border-bottom: 1px solid var(--border);
}
@media (max-width: 600px) {
  .header-inner { padding: 0 4px; }
  h1 { font-size: 16px; }
  main { padding: 0 8px; }
  .work-card { padding: 12px; }
  .work-title { font-size: 14px; }
  .question { font-size: 13px; }
  .fab .btn { padding: 10px 16px; }
}
</style>
</head>
<body>
<header>
  <div class="header-inner">
    <h1>📋 海山丈司 — 嗜好アラインメント記入フォーム</h1>
    <div class="controls">
      <input class="search" type="text" id="search" placeholder="作品名・著者で検索..." />
      <button class="filter-chip active" data-genre="all">全ジャンル</button>
      <button class="filter-chip" data-genre="manga">漫画</button>
      <button class="filter-chip" data-genre="books">本</button>
      <button class="filter-chip" data-genre="movies">映画</button>
      <button class="filter-chip" data-genre="music">音楽</button>
      <button class="filter-chip" data-filter="unanswered">未完答のみ</button>
      <button class="filter-chip" data-filter="untouched">未着手のみ</button>
      <button class="filter-chip" data-filter="high">重要度 ★4+</button>
      <button class="filter-chip" data-filter="no-importance">重要度未設定</button>
    </div>
    <div class="progress-bar"><div class="progress-fill" id="progress-fill"></div></div>
    <div class="progress-text" id="progress-text">0 / 0 回答</div>
  </div>
</header>

<main id="main"></main>

<div class="fab">
  <button class="btn btn-primary" id="download-btn">💾 JSON ダウンロード</button>
  <button class="btn btn-secondary" id="upload-btn">📂 JSON 読み込み</button>
  <input type="file" id="upload-input" accept=".json" style="display:none" />
</div>

<script>
const DATA = __DATA_JSON__;
const STANDARD_QUESTIONS = __STD_Q_JSON__;
const Q_KEYS = ["q1", "q2", "q3", "q4", "q5", "q6", "q7"];  // ★ 2026-05-17: q6/q7 追加
const STORAGE_KEY = "umiyama_hobbies_alignment_v3";  // ★ v3 (q1-q7)
const STORAGE_KEY_V2 = "umiyama_hobbies_alignment_v2";
const STORAGE_KEY_V1 = "umiyama_hobbies_alignment_v1";
const GENRE_LABELS = { manga: "漫画", books: "本", movies: "映画", music: "音楽" };

let answers = loadAnswers();
let currentGenre = "all";
let currentFilter = "all";
let currentSearch = "";

function normalizeAnswer(a) {
  // どの旧 schema からも v3 (q1-q7 + importance + comment) に揃える
  const out = {
    importance: a.importance || 0,
    q1: a.q1 || a.selected || [],
    q2: a.q2 || [],
    q3: a.q3 || [],
    q4: a.q4 || [],
    q5: a.q5 || [],
    q6: a.q6 || [],
    q7: a.q7 || [],
    comment: a.comment || "",
    last_updated: a.last_updated || ""
  };
  return out;
}

function loadAnswers() {
  try {
    // v3 を試す
    const s = localStorage.getItem(STORAGE_KEY);
    if (s) {
      const parsed = JSON.parse(s);
      const out = {};
      for (const id in parsed) out[id] = normalizeAnswer(parsed[id]);
      return out;
    }
    // v2 → v3 migration (q6/q7 を空で補完)
    const v2 = localStorage.getItem(STORAGE_KEY_V2);
    if (v2) {
      const parsed = JSON.parse(v2);
      const migrated = {};
      for (const id in parsed) migrated[id] = normalizeAnswer(parsed[id]);
      localStorage.setItem(STORAGE_KEY, JSON.stringify(migrated));
      return migrated;
    }
    // v1 → v3 migration
    const v1 = localStorage.getItem(STORAGE_KEY_V1);
    if (v1) {
      const parsed = JSON.parse(v1);
      const migrated = {};
      for (const id in parsed) migrated[id] = normalizeAnswer(parsed[id]);
      localStorage.setItem(STORAGE_KEY, JSON.stringify(migrated));
      return migrated;
    }
    return {};
  } catch { return {}; }
}

function saveAnswers() {
  localStorage.setItem(STORAGE_KEY, JSON.stringify(answers));
  updateProgress();
}

function getAnswer(workId) {
  if (!answers[workId]) {
    answers[workId] = normalizeAnswer({});
  } else {
    // 既存だが q6/q7 が無いケース (=v2 直接ロード) を補完
    answers[workId] = normalizeAnswer(answers[workId]);
  }
  return answers[workId];
}

function isPartiallyAnswered(workId) {
  const a = answers[workId];
  if (!a) return false;
  if ((a.importance || 0) > 0) return true;
  if (a.comment && a.comment.trim()) return true;
  for (const q of Q_KEYS) {
    if ((a[q] || []).length > 0) return true;
  }
  return false;
}

function isFullyAnswered(workId) {
  const a = answers[workId];
  if (!a) return false;
  if (!(a.importance || 0)) return false;
  for (const q of Q_KEYS) {
    if (!(a[q] || []).length) return false;
  }
  return true;
}

function updateProgress() {
  const total = DATA.works.length;
  const partial = DATA.works.filter(w => isPartiallyAnswered(w.id)).length;
  const full = DATA.works.filter(w => isFullyAnswered(w.id)).length;
  const pct = total ? (full / total * 100) : 0;
  document.getElementById("progress-fill").style.width = pct + "%";
  // Importance distribution
  const impDist = {0:0, 1:0, 2:0, 3:0, 4:0, 5:0};
  DATA.works.forEach(w => {
    const imp = (answers[w.id]?.importance || 0);
    impDist[imp] = (impDist[imp] || 0) + 1;
  });
  const impSummary = [5,4,3,2,1].map(v => `★${v}:${impDist[v]}`).join(" ");
  document.getElementById("progress-text").textContent =
    `完答 ${full} / ${total} (${pct.toFixed(1)}%) ・ 着手 ${partial} ・ 重要度 [${impSummary}]`;
}

function renderQuestionBlock(workId, qKey, qLabel, options) {
  const a = getAnswer(workId);
  const selected = a[qKey] || [];
  const optionsHtml = options.map(opt => {
    const checked = selected.includes(opt.key) ? "checked" : "";
    return `
      <label class="option">
        <input type="checkbox" data-work="${workId}" data-q="${qKey}" data-key="${opt.key}" ${checked} />
        <span class="option-key">(${opt.key})</span>
        <span class="option-label">${escapeHtml(opt.label)}</span>
      </label>
    `;
  }).join("");
  const labelPrefix = qKey.toUpperCase() + ".";  // "Q1.", "Q2.", ..., "Q7."
  // ★ 2026-05-17: Q6/Q7 と Q1 は複数可 (Q2 だけ単一選択イメージ)
  //   ラベルに「(複数可)」が無い場合だけ自動付与 (Q3/Q4/Q5 標準は既に明記済み)
  const multiOk = (qKey !== "q2") && !/複数可/.test(qLabel);
  const displayLabel = multiOk ? `${qLabel} (複数可)` : qLabel;
  const cls = selected.length > 0 ? "question-block answered" : "question-block";
  return `
    <div class="${cls}">
      <div class="question">${labelPrefix} ${escapeHtml(displayLabel)}</div>
      <div class="options">${optionsHtml}</div>
    </div>
  `;
}

function renderWorkCard(work) {
  const a = getAnswer(work.id);
  const partial = isPartiallyAnswered(work.id);
  const full = isFullyAnswered(work.id);

  // ★ 2026-05-17: Q1 (作品固有) → Q2-Q5 (標準) → Q6/Q7 (作品固有・深掘り) の順
  const q = work.questions || {};
  let blocks = "";
  if (q.q1) {
    blocks += renderQuestionBlock(work.id, "q1", q.q1.question, q.q1.options);
  }
  for (const sq of STANDARD_QUESTIONS) {
    blocks += renderQuestionBlock(work.id, sq.key, sq.label, sq.options);
  }
  if (q.q6) {
    blocks += renderQuestionBlock(work.id, "q6", q.q6.question, q.q6.options);
  }
  if (q.q7) {
    blocks += renderQuestionBlock(work.id, "q7", q.q7.question, q.q7.options);
  }

  // Q-state dots (重要度 + Q1-Q7 = 8 dots)
  const importanceDot = `<span class="dot ${(a.importance || 0) > 0 ? 'filled' : ''}" title="重要度: ${a.importance ? '★' + a.importance : '未'}">★</span>`;
  const dots = Q_KEYS.map(q => {
    const filled = (a[q] || []).length > 0;
    return `<span class="dot ${filled ? 'filled' : ''}" title="${q.toUpperCase()}: ${filled ? '回答済' : '未'}">${q.toUpperCase()}</span>`;
  }).join("");

  const stateCls = full ? "full" : (partial ? "partial" : "");

  // Stars block
  const starsHtml = [1,2,3,4,5].map(v => {
    const filled = (a.importance || 0) >= v;
    return `<span class="star ${filled ? 'filled' : ''}" data-work="${work.id}" data-value="${v}">★</span>`;
  }).join("");
  const impValue = a.importance ? `★${a.importance}/5` : "未設定";

  return `
    <div class="work-card ${stateCls}" data-work="${work.id}" data-genre="${work.genre}">
      <div class="work-header">
        <span class="genre-tag ${work.genre}">${GENRE_LABELS[work.genre]}</span>
        <h3 class="work-title">${escapeHtml(work.title)}</h3>
        <span class="work-meta">${escapeHtml(work.author || '')}${work.year ? ' / ' + work.year : ''}</span>
        <span class="dots">${importanceDot}${dots}</span>
      </div>
      <div class="importance-row">
        <span class="importance-label">⭐ 重要度 (好き度):</span>
        <div class="stars">${starsHtml}</div>
        <span class="importance-value" data-work="${work.id}">${impValue}</span>
        ${a.importance ? `<span class="importance-clear" data-work="${work.id}">クリア</span>` : ''}
      </div>
      ${blocks}
      <div class="comment-label">💬 コメント (自由記入)</div>
      ${work.comment_prompt ? `<div class="comment-prompt">${escapeHtml(work.comment_prompt)}</div>` : ''}
      <textarea data-work="${work.id}" placeholder="経営判断・人生節目で引用したエピソード、特に響いたフレーズ等">${escapeHtml(a.comment || '')}</textarea>
      <div class="save-indicator" data-work="${work.id}">✓ 保存済</div>
    </div>
  `;
}

function escapeHtml(s) {
  if (!s) return '';
  return String(s)
    .replace(/&/g, "&amp;")
    .replace(/</g, "&lt;")
    .replace(/>/g, "&gt;")
    .replace(/"/g, "&quot;")
    .replace(/'/g, "&#39;");
}

function render() {
  const main = document.getElementById("main");
  const works = DATA.works.filter(w => {
    if (currentGenre !== "all" && w.genre !== currentGenre) return false;
    if (currentFilter === "unanswered" && isFullyAnswered(w.id)) return false;
    if (currentFilter === "untouched" && isPartiallyAnswered(w.id)) return false;
    if (currentFilter === "high") {
      const imp = (answers[w.id]?.importance || 0);
      if (imp < 4) return false;
    }
    if (currentFilter === "no-importance") {
      const imp = (answers[w.id]?.importance || 0);
      if (imp > 0) return false;
    }
    if (currentSearch) {
      const q = currentSearch.toLowerCase();
      const hay = (w.title + " " + (w.author || "") + " " + (w.tags || "")).toLowerCase();
      if (!hay.includes(q)) return false;
    }
    return true;
  });

  if (works.length === 0) {
    main.innerHTML = '<div class="empty-state">該当する作品がありません</div>';
    return;
  }

  // Group by genre if showing all
  if (currentGenre === "all") {
    const groups = {};
    works.forEach(w => {
      if (!groups[w.genre]) groups[w.genre] = [];
      groups[w.genre].push(w);
    });
    let html = '';
    ["manga", "books", "movies", "music"].forEach(g => {
      if (!groups[g]) return;
      html += `<div class="genre-section"><h2 class="genre-section-title">${GENRE_LABELS[g]} (${groups[g].length})</h2>`;
      html += groups[g].map(renderWorkCard).join("");
      html += `</div>`;
    });
    main.innerHTML = html;
  } else {
    main.innerHTML = works.map(renderWorkCard).join("");
  }

  // Bind events
  main.querySelectorAll('input[type="checkbox"]').forEach(cb => {
    cb.addEventListener('change', e => {
      const workId = e.target.dataset.work;
      const qKey = e.target.dataset.q;
      const optKey = e.target.dataset.key;
      const a = getAnswer(workId);
      const arr = a[qKey] || [];
      if (e.target.checked) {
        if (!arr.includes(optKey)) arr.push(optKey);
      } else {
        const i = arr.indexOf(optKey);
        if (i >= 0) arr.splice(i, 1);
      }
      a[qKey] = arr;
      a.last_updated = new Date().toISOString();
      saveAnswers();
      flashSaved(workId);
      // Update visual states
      const block = e.target.closest('.question-block');
      if (block) block.classList.toggle('answered', arr.length > 0);
      const card = e.target.closest('.work-card');
      if (card) {
        card.classList.toggle('partial', isPartiallyAnswered(workId) && !isFullyAnswered(workId));
        card.classList.toggle('full', isFullyAnswered(workId));
      }
      // Update dot
      const dot = card?.querySelector(`.dot[title^="${qKey.toUpperCase()}"]`);
      if (dot) {
        dot.classList.toggle('filled', arr.length > 0);
        dot.title = `${qKey.toUpperCase()}: ${arr.length > 0 ? '回答済' : '未'}`;
      }
    });
  });
  // Star (importance) clicks
  main.querySelectorAll('.star').forEach(s => {
    s.addEventListener('click', e => {
      const workId = e.target.dataset.work;
      const value = parseInt(e.target.dataset.value);
      const a = getAnswer(workId);
      a.importance = (a.importance === value) ? 0 : value;
      a.last_updated = new Date().toISOString();
      saveAnswers();
      flashSaved(workId);
      // Re-render stars in this card
      const card = e.target.closest('.work-card');
      card?.querySelectorAll('.star').forEach(s2 => {
        const v = parseInt(s2.dataset.value);
        s2.classList.toggle('filled', a.importance >= v);
      });
      const valDisplay = card?.querySelector(`.importance-value[data-work="${workId}"]`);
      if (valDisplay) valDisplay.textContent = a.importance ? `★${a.importance}/5` : "未設定";
      // Toggle clear link
      const row = card?.querySelector('.importance-row');
      let clearLink = row?.querySelector('.importance-clear');
      if (a.importance && !clearLink) {
        clearLink = document.createElement('span');
        clearLink.className = 'importance-clear';
        clearLink.dataset.work = workId;
        clearLink.textContent = 'クリア';
        row.appendChild(clearLink);
        bindClearLink(clearLink);
      } else if (!a.importance && clearLink) {
        clearLink.remove();
      }
      // Importance dot
      const impDot = card?.querySelector('.dot[title^="重要度"]');
      if (impDot) {
        impDot.classList.toggle('filled', a.importance > 0);
        impDot.title = `重要度: ${a.importance ? '★' + a.importance : '未'}`;
      }
      // Card state
      if (card) {
        card.classList.toggle('partial', isPartiallyAnswered(workId) && !isFullyAnswered(workId));
        card.classList.toggle('full', isFullyAnswered(workId));
      }
    });
  });

  function bindClearLink(el) {
    el.addEventListener('click', e => {
      const workId = e.target.dataset.work;
      const a = getAnswer(workId);
      a.importance = 0;
      a.last_updated = new Date().toISOString();
      saveAnswers();
      flashSaved(workId);
      const card = e.target.closest('.work-card');
      card?.querySelectorAll('.star').forEach(s2 => s2.classList.remove('filled'));
      const valDisplay = card?.querySelector(`.importance-value[data-work="${workId}"]`);
      if (valDisplay) valDisplay.textContent = "未設定";
      e.target.remove();
      const impDot = card?.querySelector('.dot[title^="重要度"]');
      if (impDot) { impDot.classList.remove('filled'); impDot.title = '重要度: 未'; }
      if (card) {
        card.classList.toggle('partial', isPartiallyAnswered(workId) && !isFullyAnswered(workId));
        card.classList.toggle('full', isFullyAnswered(workId));
      }
    });
  }
  main.querySelectorAll('.importance-clear').forEach(bindClearLink);

  main.querySelectorAll('textarea').forEach(ta => {
    ta.addEventListener('input', e => {
      const workId = e.target.dataset.work;
      const a = getAnswer(workId);
      a.comment = e.target.value;
      a.last_updated = new Date().toISOString();
      clearTimeout(ta._saveTimer);
      ta._saveTimer = setTimeout(() => {
        saveAnswers();
        flashSaved(workId);
        const card = ta.closest('.work-card');
        if (card) {
          card.classList.toggle('partial', isPartiallyAnswered(workId) && !isFullyAnswered(workId));
          card.classList.toggle('full', isFullyAnswered(workId));
        }
      }, 400);
    });
  });
}

function flashSaved(workId) {
  const ind = document.querySelector(`.save-indicator[data-work="${workId}"]`);
  if (!ind) return;
  ind.classList.add('show');
  clearTimeout(ind._t);
  ind._t = setTimeout(() => ind.classList.remove('show'), 1500);
}

// Filter chips
document.querySelectorAll('.filter-chip').forEach(chip => {
  chip.addEventListener('click', () => {
    if (chip.dataset.genre) {
      document.querySelectorAll('.filter-chip[data-genre]').forEach(c => c.classList.remove('active'));
      chip.classList.add('active');
      currentGenre = chip.dataset.genre;
    } else if (chip.dataset.filter) {
      const wasActive = chip.classList.contains('active');
      document.querySelectorAll('.filter-chip[data-filter]').forEach(c => c.classList.remove('active'));
      if (!wasActive) {
        chip.classList.add('active');
        currentFilter = chip.dataset.filter;
      } else {
        currentFilter = 'all';
      }
    }
    render();
  });
});

document.getElementById("search").addEventListener('input', e => {
  currentSearch = e.target.value.trim();
  clearTimeout(window._searchTimer);
  window._searchTimer = setTimeout(render, 150);
});

// Download
document.getElementById("download-btn").addEventListener('click', () => {
  const total = DATA.works.length;
  const partial = DATA.works.filter(w => isPartiallyAnswered(w.id)).length;
  const full = DATA.works.filter(w => isFullyAnswered(w.id)).length;
  const impDist = {0:0,1:0,2:0,3:0,4:0,5:0};
  DATA.works.forEach(w => {
    const imp = (answers[w.id]?.importance || 0);
    impDist[imp] = (impDist[imp] || 0) + 1;
  });
  const payload = {
    schema_version: "v2-with-importance",
    exported_at: new Date().toISOString(),
    form_version: DATA.version,
    total_works: total,
    answered_partial: partial,
    answered_full: full,
    progress_pct: total ? Math.round(full / total * 1000) / 10 : 0,
    importance_distribution: impDist,
    _note: "重要度 (importance: 1-5) は Wiki 反映時の重み付けに使用。★5 = 中核作品 (frontmatter に importance: 5 + clone_visibility: public、retrieval 優先)、★1-2 = 周辺 / 参考。未設定は重み付け中立。",
    answers: answers
  };
  const blob = new Blob([JSON.stringify(payload, null, 2)], { type: 'application/json' });
  const url = URL.createObjectURL(blob);
  const a = document.createElement('a');
  const ts = new Date().toISOString().slice(0, 10);
  a.href = url;
  a.download = `umiyama_hobbies_alignment_${ts}.json`;
  document.body.appendChild(a);
  a.click();
  document.body.removeChild(a);
  URL.revokeObjectURL(url);
});

// Upload
document.getElementById("upload-btn").addEventListener('click', () => {
  document.getElementById("upload-input").click();
});
document.getElementById("upload-input").addEventListener('change', e => {
  const file = e.target.files[0];
  if (!file) return;
  const reader = new FileReader();
  reader.onload = () => {
    try {
      const data = JSON.parse(reader.result);
      if (!data.answers) {
        alert("JSONフォーマットが正しくありません (answers キーが必要)");
        return;
      }
      const yes = confirm(`${Object.keys(data.answers).length} 件の回答をロードします。既存の回答は上書きされます。続行?`);
      if (!yes) return;
      // ★ normalize each entry so older format (q1-q5 only) gets q6/q7 auto-populated as empty
      answers = {};
      for (const id in data.answers) {
        answers[id] = normalizeAnswer(data.answers[id]);
      }
      saveAnswers();
      render();
      alert(`✓ ${Object.keys(answers).length} 件の回答をロードしました`);
    } catch (err) {
      alert("読み込みに失敗しました: " + err.message);
    }
  };
  reader.readAsText(file);
  e.target.value = '';
});

// Initial render
updateProgress();
render();
</script>
</body>
</html>
"""


def main():
    print(f"Reading wiki from: {WIKI_DIR}")
    works = collect_works()
    print(f"Extracted {len(works)} works:")
    for g in ["manga", "books", "movies", "music"]:
        c = sum(1 for w in works if w["genre"] == g)
        print(f"  {g}: {c}")

    data = {
        "version": datetime.now().strftime("%Y-%m-%d"),
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "total": len(works),
        "works": works,
    }

    OUT_DIR.mkdir(parents=True, exist_ok=True)

    # Embed JSON into HTML (escape </script> just in case)
    def safe_json(obj):
        return json.dumps(obj, ensure_ascii=False).replace("</script>", "<\\/script>")

    html = HTML_TEMPLATE.replace("__DATA_JSON__", safe_json(data))
    html = html.replace("__STD_Q_JSON__", safe_json(STANDARD_QUESTIONS))

    OUT_FILE.write_text(html, encoding="utf-8")
    size_kb = OUT_FILE.stat().st_size / 1024
    print(f"\n✓ Generated: {OUT_FILE}")
    print(f"  Size: {size_kb:.1f} KB")
    print(f"  Open: open '{OUT_FILE}'")


if __name__ == "__main__":
    main()
