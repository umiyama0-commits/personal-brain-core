"""
build_fine_tune_dataset.py — うみやまAI fine-tune 用 dataset 抽出 + 品質フィルタ + 集計
                              (★2026-05-23 海山指示 A の準備)

設計:
  「規則積み上げ」を超える本人像複製の打ち手 A (fine-tune) のため、
  既存の海山発言データを **OpenAI fine-tune jsonl 形式** に整形する。

source 4 種類:
  1. clone_history/*.jsonl       — LW DM の user 質問 + assistant (bot) 応答 ペア
  2. alignment_trial/runs/*_reviewed.json — 海山が手動レビューした response
                                            (verdict=ok / fix + edited_response)
  3. alignment_history.json     — 100 問 alignment 回答 (= 海山が直接書いた answer)
  4. clone_improve/response_quality/*.jsonl — Layer 1 judge スコア (= 採点済 turn)

品質フィルタ:
  - response_quality_judge の min(ai_smell, mirroring_fit, length_appropriate) >= 3
  - alignment_trial verdict in ("ok", "fix") のみ採用 (= reject / 未レビューは除外)
  - response 短すぎ (< 30 字) / fallback 文言 prefix は除外
  - response_quality 採点が無い turn は「未採点」flag で含めるが、デフォルト除外

出力:
  data/brain/fine_tune/dataset_v<N>.jsonl   — OpenAI fine-tune 形式
  data/brain/fine_tune/report_v<N>.md       — 集計レポート (件数 / source / quality / 長さ)

OpenAI fine-tune 形式:
  {"messages": [
    {"role": "system", "content": "あなたは海山丈司..."},
    {"role": "user", "content": "質問内容"},
    {"role": "assistant", "content": "海山的応答"}
  ]}

実行:
  python3 scripts/build_fine_tune_dataset.py             # dataset 抽出 (デフォルト出力)
  python3 scripts/build_fine_tune_dataset.py --report    # 集計レポートのみ表示
  python3 scripts/build_fine_tune_dataset.py --version v2 # 出力 version 指定
  python3 scripts/build_fine_tune_dataset.py --include-unscored # 未採点 turn も含める
  python3 scripts/build_fine_tune_dataset.py --min-quality 4 # quality 閾値変更 (default 3)
"""
from __future__ import annotations

import argparse
import hashlib
import json
import logging
import os
import re
import sys
from collections import defaultdict, Counter
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import Iterator, Optional

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger("build_fine_tune_dataset")

JST = timezone(timedelta(hours=9))
APP_ROOT = Path(os.getenv("BRAIN_APP_ROOT", "/app"))
DATA_BRAIN = APP_ROOT / "data" / "brain"

HISTORY_DIR = DATA_BRAIN / "clone_history"
ALIGNMENT_TRIAL_DIR = DATA_BRAIN / "clone_improve" / "alignment_trial" / "runs"
ALIGNMENT_HISTORY = DATA_BRAIN / "alignment_history.json"
QUALITY_DIR = DATA_BRAIN / "clone_improve" / "response_quality"
INTERVIEW_WIKI_DIR = DATA_BRAIN / "wiki" / "interview"     # ★Vapi 蒸留採用済 8 カテゴリ
RAW_NOTES_DIR = DATA_BRAIN / "raw" / "notes"               # ★海山 /teach /forward 直書きメモ
ALIGNMENT_DIR = DATA_BRAIN / "alignment"                    # ★各種 Q&A JSON 群 (= 100 問 / 50 問 / clone qa / vmd 等)
WIKI_DIR = DATA_BRAIN / "wiki"                              # ★identity / style / thinking compile 出力
MEETINGS_WIKI_DIR = DATA_BRAIN / "wiki" / "meetings"        # ★2026-05-23 plaud 議事録 LLM compile 済
OUT_DIR = DATA_BRAIN / "fine_tune"

# fallback 文言 (= 応答失敗時、dataset から除外)
FALLBACK_PREFIXES = (
    "お休みをいただいてます",
    "申し訳ありません。少し時間",
    "[error]",
)

# fine-tune に使う system message (= 簡潔版、CLONE_PUBLIC_PROMPT_STATIC の核を凝縮)
# fine-tune では「重み」に内在化させるため、prompt はミニマル
SYSTEM_PROMPT_FOR_TUNING = (
    "あなたは OWNDAYS 社長・海山丈司の AI 分身「うみやまAI」。"
    "海山として喋る (= AI 臭さを出さない、規則の網羅でなく本人の温度感で返す)。"
    "短い問いには短く、深い問いには深く、ミラーリングを守る。"
    "業務データは数字を丁寧に、相談は受け止め 1 + 視点 1 + 問い 1 で完結させる。"
)

# 応答最低字数 (= ★2026-05-23 2 度目 fix):
# - clone_history (= bot 応答): 30 字 (= 相槌のみ除外、AI 自動生成のばらつき抑制)
# - alignment 系 (= 海山直答): 4 字 (= 「自由」「肉」「面白いか」等の choice 短答も gold)
#   ※ choice-based alignment (= alignment_100 / 50 / answers / ops / vmd) は
#     「人生で最も大切?」→「自由」型の判断パターン、極短でも fine-tune 価値あり
MIN_SUBSTANTIVE_CHARS_BOT = int(os.getenv("FINE_TUNE_MIN_CHARS_BOT", "30"))
MIN_SUBSTANTIVE_CHARS_ALIGNMENT = int(os.getenv("FINE_TUNE_MIN_CHARS_ALIGNMENT", "4"))


# ─── 機密 keyword 統一 (★2026-05-23 Reviewer agent 指摘の structural defect fix) ─
# 全 iter で必ず使う、漏れたら fine-tune model に embed → 推論で漏出する事故経路あり。
# meetings / LW で別 list 持ってた DRY 違反も統合。
_SENSITIVE_KEYWORDS_UNIFIED = (
    # M&A / 買収 / 投資判断
    "M&A", "Ｍ＆Ａ", "買収", "売却交渉", "未公開情報", "デューデリ",
    "資金調達", "増資", "新株予約権",
    # 人事 / 給与 / 考課 (= 個人特定 + 待遇情報)
    "人事評価", "考課", "査定", "給与", "賞与", "報酬", "処遇",
    "解雇", "退職金", "ストックオプション", "SO付与", "減給",
    "昇給査定",  # 「昇給方針」自体は OK だが「昇給査定」は個別
    # 機密 marker
    "機密", "極秘", "社外秘", "confidential",
)


def _text_has_sensitive(text: str) -> bool:
    """全 iter 共通の機密判定。clone_history / alignment_* / personal_line 等で必ず呼ぶ。"""
    if not text:
        return False
    return any(kw in text for kw in _SENSITIVE_KEYWORDS_UNIFIED)


# ─── 品質判定 ─────────────────────────────────
def _is_substantive(text: str, min_chars: int = MIN_SUBSTANTIVE_CHARS_BOT) -> bool:
    """fallback / 短すぎる応答を除外。

    Args:
        text: 判定対象
        min_chars: 最低字数 (= 30 default for bot、alignment 系は 15 を渡す)
    """
    if not text or len(text.strip()) < min_chars:
        return False
    t = text.strip()
    return not any(t.startswith(p) for p in FALLBACK_PREFIXES)


# ─── source 1: clone_history pair 抽出 ─────────────
def iter_history_pairs() -> Iterator[dict]:
    """clone_history/<user_id>.jsonl から user-assistant pair を抽出。

    Yields: {
      "user": str, "assistant": str, "ts": str,
      "user_id": str, "source": "clone_history",
    }
    """
    if not HISTORY_DIR.exists():
        return
    for f in sorted(HISTORY_DIR.glob("*.jsonl")):
        try:
            lines = f.read_text(encoding="utf-8").splitlines()
        except Exception:
            continue
        records = []
        for line in lines:
            line = line.strip()
            if not line:
                continue
            try:
                r = json.loads(line)
                records.append(r)
            except Exception:
                continue
        # ts 順 sort
        records.sort(key=lambda x: x.get("timestamp", ""))
        # user → assistant の隣接 pair
        for i in range(len(records) - 1):
            a, b = records[i], records[i + 1]
            if a.get("role") == "user" and b.get("role") == "assistant":
                user_text = (a.get("text") or "").strip()
                bot_text = (b.get("text") or "").strip()
                if not user_text or not _is_substantive(bot_text):
                    continue
                # ★2026-05-23 Reviewer 指摘: 機密 keyword filter 全 iter 必須
                if _text_has_sensitive(user_text) or _text_has_sensitive(bot_text):
                    continue
                yield {
                    "user": user_text,
                    "assistant": bot_text,
                    "ts": b.get("timestamp", ""),
                    "user_id": a.get("user_id", ""),
                    "source": "clone_history",
                }


# ─── source 2: alignment_trial review 抽出 ─────────────
def iter_alignment_trial_pairs() -> Iterator[dict]:
    """alignment_trial の reviewed.json から海山採用 / 修正版 response を抽出。

    verdict=ok    → (scenario, response) を採用 (= bot 応答が gold)
    verdict=fix   → (scenario or edited_question, edited_response) を採用 (= 海山書き直し)
    verdict=reject/未レビュー → 除外
    """
    if not ALIGNMENT_TRIAL_DIR.exists():
        return
    for f in sorted(ALIGNMENT_TRIAL_DIR.glob("*_reviewed.json")):
        try:
            run = json.loads(f.read_text(encoding="utf-8"))
        except Exception:
            continue
        results = run.get("results", []) if isinstance(run, dict) else []
        run_id = run.get("run_id", f.stem.replace("_reviewed", ""))
        for r in results:
            verdict = (r.get("verdict") or "").strip()
            if verdict not in ("ok", "fix"):
                continue
            # user message: 修正済 question があればそれ、なければ scenario
            user_text = (r.get("edited_question") or r.get("scenario") or "").strip()
            # assistant message: verdict=fix なら edited_response、ok なら raw response
            if verdict == "fix":
                bot_text = (r.get("edited_response") or "").strip()
                if not bot_text:
                    # fix だが edited_response 無い → 海山書き直し未完、除外
                    continue
            else:  # ok
                bot_text = (r.get("response") or "").strip()
            if not user_text or not _is_substantive(bot_text):
                continue
            # ★2026-05-23 Reviewer 指摘: 機密 keyword filter 適用
            if _text_has_sensitive(user_text) or _text_has_sensitive(bot_text):
                continue
            yield {
                "user": user_text,
                "assistant": bot_text,
                "ts": r.get("ts", ""),
                "user_id": f"alignment_trial:{r.get('id', '?')}",
                "source": f"alignment_trial:{run_id}:{verdict}",
            }


# ─── source 3: alignment_history (100 問海山回答) ─────────────
def iter_alignment_history_pairs() -> Iterator[dict]:
    """alignment_history.json の 100 問 alignment 回答 (= 海山が直接書いた answer)。

    ★2026-05-23 field 名 fix: 実際の構造は {date, category, question, intent, answer_summary}。
    旧コードは "answer" を期待してて 0 件カウントしてた bug を修正。
    """
    if not ALIGNMENT_HISTORY.exists():
        return
    try:
        data = json.loads(ALIGNMENT_HISTORY.read_text(encoding="utf-8"))
    except Exception:
        return
    # data 構造は list of {question, answer_summary, date, category, intent, ...}
    # alignment_question.py 経由で書かれる、answer_summary が本文
    records = data if isinstance(data, list) else data.get("entries", [])
    for r in records:
        user_text = (r.get("question") or "").strip()
        # answer_summary (= 海山が直接書いた回答) を本命、answer は legacy fallback
        bot_text = (
            (r.get("answer_summary") or r.get("answer") or "").strip()
        )
        # alignment 系は短い直答 (= 「肉。」「80%。」等) も gold standard で採用
        if not user_text or not _is_substantive(bot_text, min_chars=MIN_SUBSTANTIVE_CHARS_ALIGNMENT):
            continue
        # ★2026-05-23 Reviewer 指摘: 機密 keyword filter 適用 (= 100 問の中で考課/給与系の質問混入対策)
        if _text_has_sensitive(user_text) or _text_has_sensitive(bot_text):
            continue
        yield {
            "user": user_text,
            "assistant": bot_text,
            "ts": r.get("date", "") or r.get("ts", "") or r.get("timestamp", ""),
            "user_id": "alignment_history",
            "source": "alignment_history",
            "category": r.get("category", ""),
            "intent": r.get("intent", ""),
        }


# ─── source 4: wiki/interview/ (Vapi 蒸留採用済 8 カテゴリ、★2026-05-23 追加) ─────────────
def iter_interview_wiki_pairs() -> Iterator[dict]:
    """wiki/interview/{biography,value-roots,judgment,emotion_reflex,aesthetics,
    relationships,embodiment,philosophy,style,reflex}.md の各エントリを pair 化。

    ★2026-05-23 真因 fix: 実 file 構造は bullet 列挙形式だった。
    fix 前は "## YYYY-MM-DD-HHMM" 見出し期待で 0 件取得。

    実 file 構造 (= alignment_interview.apply_extraction 出力):
      # {category} (雑談アラインメント由来)

      海山が車内などで AI と雑談した内容から蒸留。海山レビュー済のみ反映。
      ...

      - [YYYY-MM-DD] (confidence) <本文> — 出典: 「<引用>」
      - [YYYY-MM-DD] (confidence) <本文> — 出典: 「<引用>」
      ...

    抽出: 行頭が `- [YYYY-MM-DD]` で始まる行を 1 bullet = 1 item として扱う。
    続く改行で開始する別 bullet までを 1 item の body に。
    """
    if not INTERVIEW_WIKI_DIR.exists():
        return
    BULLET_RE = re.compile(r"^- \[(\d{4}-\d{2}-\d{2})\]\s*(?:\(([^)]+)\)\s*)?(.+)$")
    # ★2026-07-03 persona-v3 cross-check DA R1 (最重要): allowlist 化。
    # v3 で interview/ に生活者・私人カテゴリ (family/shadow/money-personal/body-health/
    # inner-voice/episodes/humor/taste-daily) が増えた。glob 全量だと深層プライベートが
    # **うみやまAI (社員向け) fine-tune の重みに焼き込まれ**、clone_visibility gate が
    # 全て無効化される (重みに visibility は無い)。学習は従来の仕事隣接カテゴリのみ。
    # v3 カテゴリの学習投入は将来 海山の明示 opt-in でのみ。
    # misc は未知カテゴリの fallback 先 = v3 系が流れ込み得るため対象外 (fail-safe)
    FT_CATEGORY_ALLOWLIST = {
        "biography", "value-roots", "judgment", "reflex", "aesthetics",
        "relationships", "embodiment", "philosophy", "style",
    }
    for f in sorted(INTERVIEW_WIKI_DIR.glob("*.md")):
        category = f.stem  # biography / value-roots / etc.
        if category not in FT_CATEGORY_ALLOWLIST:
            continue  # v3 深層カテゴリは fine-tune 非投入 (fail-safe)
        try:
            content = f.read_text(encoding="utf-8")
        except Exception:
            continue
        # bullet を 1 行ずつ抽出 (= 蒸留 item は 1 行で完結する想定)
        for line in content.splitlines():
            m = BULLET_RE.match(line.rstrip())
            if not m:
                continue
            ts = m.group(1)               # YYYY-MM-DD
            confidence = (m.group(2) or "").strip()  # high / medium / low (= optional)
            body = m.group(3).strip()     # 本文 (= 「— 出典: ...」を含む可能性)
            if not _is_substantive(body, min_chars=MIN_SUBSTANTIVE_CHARS_ALIGNMENT):
                continue
            # user query を合成 (= category 別 jp 質問)
            category_jp = {
                "biography": "経歴 / 過去の出来事",
                "value-roots": "価値観の根っこ",
                "judgment": "判断パターン",
                "judgment_reflex": "判断・反射パターン",
                "emotion-reflex": "感情の反射",
                "emotion_reflex": "感情の反射",
                "aesthetics": "美意識 / こだわり",
                "relationships": "人間関係 / 距離感",
                "embodiment": "身体性 / 感覚",
                "philosophy": "哲学 / 世界観",
                "style": "言語スタイル",
                "reflex": "反射的反応",
            }.get(category, category)
            user_text = f"{category_jp} について、海山さんの本人像を聞かせて。"
            yield {
                "user": user_text,
                "assistant": body,
                "ts": ts,
                "user_id": f"interview:{category}",
                "source": f"wiki_interview:{category}",
                "category": category,
                "confidence": confidence,
            }


# ─── source 5: raw/notes/ の alignment / align / mogumogu deliberate メモ (★2026-05-23 追加) ─
def iter_raw_notes_alignment_pairs() -> Iterator[dict]:
    """raw/notes/ から海山が deliberate に書いた alignment / 定期執筆系メモを抽出。

    対象 prefix:
      - alignment_*.md (= alignment 100 問 / 50 問の回答メモ)
      - align_*.md (= 組織 / 業務 alignment の海山メモ)
      - mogumog_Vol_*.md (= 海山タケシ社長のもぐもぐダイアリー、★2026-05-23 海山指示)
                          STAPA OWNDAYS MAGAZINE 連載、海山定期執筆

    除外: apple_notes_*.md / lineworks_*.md / stapa_*.md / gdrive_*.md /
          chat_*.md / claude_*.md / onmaga_batch_*.md (= 第三者著作混在の疑い)

    file は 1 file 全体を 1 item として扱う (= 海山が 1 つのテーマで書いたメモ)。
    """
    if not RAW_NOTES_DIR.exists():
        return
    ACCEPTED_PREFIXES = ("alignment_", "align_", "mogumog_")
    EXCLUDED_PREFIXES = (
        "apple_notes_", "lineworks_", "stapa_", "gdrive_",
        "chat_", "claude_", "chatgpt_", "_processed",
        "onmaga_batch_",  # ★混在の疑い、別途確認後に追加判断
    )
    for f in sorted(RAW_NOTES_DIR.glob("*.md")):
        if not any(f.name.startswith(p) for p in ACCEPTED_PREFIXES):
            continue
        if any(f.name.startswith(p) for p in EXCLUDED_PREFIXES):
            continue
        try:
            content = f.read_text(encoding="utf-8")
        except Exception:
            continue
        # frontmatter (= --- で区切られた前部) を除外
        if content.startswith("---\n"):
            parts = content.split("---\n", 2)
            if len(parts) >= 3:
                content = parts[2]
        body = content.strip()
        if not _is_substantive(body, min_chars=MIN_SUBSTANTIVE_CHARS_ALIGNMENT):
            continue
        # title を 1 行目から抽出 (= 「# Title」or 自然 1 行)
        first_line = body.split("\n", 1)[0].strip()
        title = first_line.lstrip("# ").strip() if first_line.startswith("#") else f.stem
        user_text = f"{title} について教えて。"
        # ts は file 名から (= alignment_100q_v2_2026-05-11.md)
        ts_match = re.search(r"(\d{4}-\d{2}-\d{2})", f.name)
        ts = ts_match.group(1) if ts_match else ""
        yield {
            "user": user_text,
            "assistant": body,
            "ts": ts,
            "user_id": f"raw_notes:{f.stem}",
            "source": f"raw_notes:{f.stem.split('_')[0]}",  # alignment or align
        }


# ─── source A1: wiki/hobbies/ 海山視点 section (★2026-05-23 海山指示) ─────────
def iter_hobbies_wiki_pairs() -> Iterator[dict]:
    """wiki/hobbies/{manga,books,movies,music,...}/*.md の海山視点 section を抽出。

    各 hobbies wiki は frontmatter + 標準セクション構造:
      # {title}
      ## § 1. 作品の核
      ## § 2. テーマの層構造
      ## § 3. 海山さんの価値観との接続  ← ★これが海山視点
      ## § 4. 名セリフ / 印象的シーン
      ## § 5. 未解明・要追加
      ## § 6. 関連リンク

    「§ 3」section 本文を assistant、frontmatter の title を user query base に。
    """
    hobbies_dir = WIKI_DIR / "hobbies"
    if not hobbies_dir.exists():
        return
    # § 3 section の見出しパターン (= 全角・半角の揺れ対応)
    SECTION_3_RE = re.compile(
        r"^##\s*(?:§\s*)?3\.?\s*海山(?:さん)?の価値観.*$",
        re.MULTILINE,
    )
    NEXT_H2_RE = re.compile(r"^##\s", re.MULTILINE)
    for f in sorted(hobbies_dir.rglob("*.md")):
        if f.name.startswith(".") or f.name == "index.md":
            continue
        try:
            content = f.read_text(encoding="utf-8")
        except Exception:
            continue
        # frontmatter から title 取得
        title = f.stem
        if content.startswith("---\n"):
            parts = content.split("---\n", 2)
            if len(parts) >= 3:
                fm = parts[1]
                for line in fm.splitlines():
                    if line.startswith("title:"):
                        title = line.split(":", 1)[1].strip().strip("\"'")
                        break
                content = parts[2]
        # § 3 section を抽出
        m = SECTION_3_RE.search(content)
        if not m:
            continue
        start = m.end()
        rest = content[start:]
        # 次の H2 まで
        m2 = NEXT_H2_RE.search(rest)
        body = (rest[:m2.start()] if m2 else rest).strip()
        if not _is_substantive(body, min_chars=MIN_SUBSTANTIVE_CHARS_ALIGNMENT):
            continue
        # category (= hobbies subdir name) + title で user query
        rel = f.relative_to(hobbies_dir)
        category = rel.parts[0] if len(rel.parts) > 1 else "general"
        yield {
            "user": f"「{title}」 ({category}) は海山さんとどう繋がる?",
            "assistant": body,
            "ts": "",
            "user_id": f"hobbies:{category}:{f.stem}",
            "source": f"wiki_hobbies:{category}",
            "category": category,
        }


# ─── source A2: wiki/style/ + wiki/judgment/ の「核となる主張」section (★2026-05-23) ─
def iter_style_judgment_core_pairs() -> Iterator[dict]:
    """wiki/style/*.md + wiki/judgment/*.md の「## 核となる主張 (本人発言)」section を抽出。

    海山が deliberate に編集した style / judgment 軸の「本人発言引用」部分。
    """
    CORE_RE = re.compile(
        r"^##\s*核となる主張.*$",
        re.MULTILINE,
    )
    NEXT_H2_RE = re.compile(r"^##\s", re.MULTILINE)
    for sub in ("style", "judgment"):
        d = WIKI_DIR / sub
        if not d.exists():
            continue
        for f in sorted(d.glob("*.md")):
            if f.name.startswith(".") or f.name == "index.md":
                continue
            try:
                content = f.read_text(encoding="utf-8")
            except Exception:
                continue
            # frontmatter 除去 + title 抽出
            title = f.stem
            if content.startswith("---\n"):
                parts = content.split("---\n", 2)
                if len(parts) >= 3:
                    content = parts[2]
            # 「核となる主張」section
            m = CORE_RE.search(content)
            if not m:
                continue
            start = m.end()
            rest = content[start:]
            m2 = NEXT_H2_RE.search(rest)
            body = (rest[:m2.start()] if m2 else rest).strip()
            if not _is_substantive(body, min_chars=MIN_SUBSTANTIVE_CHARS_ALIGNMENT):
                continue
            yield {
                "user": f"「{title}」について、海山さんが大事にしてる本人の主張は?",
                "assistant": body,
                "ts": "",
                "user_id": f"{sub}:{f.stem}",
                "source": f"wiki_{sub}_core:{f.stem}",
                "category": sub,
            }


# ─── source A3: wiki/decisions/ + system_improvements/ + meta (★2026-05-23) ─────
def iter_decisions_and_meta_pairs() -> Iterator[dict]:
    """wiki/decisions/*.md + system_improvements/*.md + meta/alignment_state.md を pair 化。

    海山 deliberate 編集の重要決定 / 改善記録 / 月次本人像 snapshot。
    1 file = 1 pair、title を user query に。
    """
    targets = []
    # wiki/decisions/
    d = WIKI_DIR / "decisions"
    if d.exists():
        for f in sorted(d.glob("*.md")):
            if not f.name.startswith(".") and f.name != "index.md":
                targets.append(("wiki_decisions", "重要決定", f))
    # system_improvements/
    si = DATA_BRAIN / "system_improvements"
    if si.exists():
        for f in sorted(si.glob("*.md")):
            if not f.name.startswith("."):
                targets.append(("system_improvements", "システム改善", f))
    # meta/alignment_state.md
    msa = DATA_BRAIN / "meta" / "alignment_state.md"
    if msa.exists():
        targets.append(("meta_alignment", "月次本人像 snapshot", msa))

    for source_prefix, label, f in targets:
        try:
            content = f.read_text(encoding="utf-8")
        except Exception:
            continue
        # frontmatter + title
        title = f.stem
        if content.startswith("---\n"):
            parts = content.split("---\n", 2)
            if len(parts) >= 3:
                fm = parts[1]
                for line in fm.splitlines():
                    if line.startswith("title:"):
                        title = line.split(":", 1)[1].strip().strip("\"'")
                        break
                content = parts[2]
        body = content.strip()
        if not _is_substantive(body, min_chars=MIN_SUBSTANTIVE_CHARS_ALIGNMENT):
            continue
        if len(body) > 8000:
            body = body[:8000] + "\n\n... (truncated)"
        yield {
            "user": f"{label}: {title}",
            "assistant": body,
            "ts": "",
            "user_id": f"{source_prefix}:{f.stem}",
            "source": source_prefix,
            "category": source_prefix,
        }


# ─── source A4: raw/notes/ deliberate prefix 拡張 (★2026-05-23) ─────────
def iter_raw_notes_deliberate_pairs() -> Iterator[dict]:
    """raw/notes/ から海山 deliberate な単一テーマメモを追加抽出。

    対象 prefix (= alignment_/align_/mogumog_ 以外で海山執筆 / deliberate なもの):
      umiyama_*.md / example_*.md / owndays_board_*.md /
      owndays_consolidated_*.md / org_chart_*.md / store_master_*.md /
      owndays_vmv_*.md

    iter_raw_notes_alignment_pairs と区別するため別 source 名 (= raw_notes_deliberate)。
    """
    if not RAW_NOTES_DIR.exists():
        return
    ACCEPTED_PREFIXES = (
        "umiyama_", "example_",
        "owndays_board_", "owndays_consolidated_",
        "owndays_vmv_", "org_chart_", "store_master_",
    )
    for f in sorted(RAW_NOTES_DIR.glob("*.md")):
        if not any(f.name.startswith(p) for p in ACCEPTED_PREFIXES):
            continue
        try:
            content = f.read_text(encoding="utf-8")
        except Exception:
            continue
        if content.startswith("---\n"):
            parts = content.split("---\n", 2)
            if len(parts) >= 3:
                content = parts[2]
        body = content.strip()
        if not _is_substantive(body, min_chars=MIN_SUBSTANTIVE_CHARS_ALIGNMENT):
            continue
        first_line = body.split("\n", 1)[0].strip()
        title = first_line.lstrip("# ").strip() if first_line.startswith("#") else f.stem
        ts_match = re.search(r"(\d{4}-\d{2}-\d{2})", f.name)
        ts = ts_match.group(1) if ts_match else ""
        yield {
            "user": f"{title} について教えて。",
            "assistant": body,
            "ts": ts,
            "user_id": f"raw_notes_deliberate:{f.stem}",
            "source": "raw_notes_deliberate",
        }


# ─── source A6: chatgpt_/claude_ AI 私的会話の海山質問パターン (★2026-05-23) ─
def iter_ai_chat_questions_pairs() -> Iterator[dict]:
    """raw/notes/chatgpt_*.md + claude_*.md から海山質問を抽出。

    fine-tune の本筋 (= 海山らしく応答するモデル) には役割逆だが、
    「海山らしい質問パターン (= どう疑問を立てるか / 何を聞きたがるか)」 として
    独立 source 化。assistant 役に「海山質問」を置き、user 役に文脈 (= title)
    を入れる構造で、海山が AI に投げる時の語彙・テンポ・関心領域を学ばせる。

    file 構造 (= claude_/chatgpt_ scraper 出力):
      [Claude.ai or ChatGPT] <title>
      <date>

      \t海山丈司\t<質問本文>

      (AI 応答が続く、これは取らない)

    抽出: `\t海山丈司\t<msg>` 行を抽出、各行 = 1 質問パターン
    """
    if not RAW_NOTES_DIR.exists():
        return
    ACCEPTED_PREFIXES = ("chatgpt_", "claude_")
    UMIYAMA_TAB_RE = re.compile(r"^\s*海山丈司\s+(.+)$")
    TITLE_RE = re.compile(r"^\[(?:Claude\.ai|ChatGPT)\]\s*(.+)$")

    for f in sorted(RAW_NOTES_DIR.glob("*.md")):
        if not any(f.name.startswith(p) for p in ACCEPTED_PREFIXES):
            continue
        try:
            content = f.read_text(encoding="utf-8")
        except Exception:
            continue
        # title 取得
        title = f.stem
        for line in content.splitlines():
            m = TITLE_RE.match(line.strip())
            if m:
                title = m.group(1).strip()
                break
        # 海山発言 (= タブ区切り) を抽出
        for line in content.splitlines():
            m = UMIYAMA_TAB_RE.match(line)
            if not m:
                continue
            question = m.group(1).strip()
            if not _is_substantive(question, min_chars=MIN_SUBSTANTIVE_CHARS_ALIGNMENT):
                continue
            # 機密 keyword skip
            if any(kw in question for kw in
                   ("機密", "M&A", "Ｍ＆Ａ", "人事評価", "考課", "給与", "賞与")):
                continue
            ts_match = re.search(r"(\d{4}-\d{2}-\d{2})", f.name)
            ts = ts_match.group(1) if ts_match else ""
            yield {
                "user": f"次のテーマで海山さんならどう質問する? テーマ: {title}",
                "assistant": question,
                "ts": ts,
                "user_id": f"ai_chat:{f.stem}",
                "source": "ai_chat_questions",
                "category": "chatgpt" if f.name.startswith("chatgpt_") else "claude",
            }


# ─── source A7/8/9: clone_learning + clone_feedback + raw/conversations (★2026-05-23 海山指示) ─
def iter_clone_learning_pairs() -> Iterator[dict]:
    """clone_learning/*.jsonl から海山採用済 (= verdict=accepted) を pair 化。

    各 record の想定 schema (= clone_learning.py 経由):
      {
        "id": "...", "category": "fact|correction|decision|style|other",
        "user_query": "...", "ai_response": "...",
        "finding": "<会話発見の本文>",  # 海山発見、本人視点 / 訂正
        "verdict": "accepted|rejected|pending",
        "ts": "..."
      }

    accepted のみ採用、finding を assistant に。
    """
    clone_learning_dir = DATA_BRAIN / "clone_learning"
    if not clone_learning_dir.exists():
        return
    for f in sorted(clone_learning_dir.glob("*.jsonl")):
        try:
            lines = f.read_text(encoding="utf-8").splitlines()
        except Exception:
            continue
        for line in lines:
            line = line.strip()
            if not line:
                continue
            try:
                r = json.loads(line)
            except Exception:
                continue
            if not isinstance(r, dict):
                continue
            if (r.get("verdict") or "").strip() not in ("accepted", "applied"):
                continue
            user_text = (r.get("user_query") or r.get("trigger") or "").strip()
            finding = (r.get("finding") or r.get("note") or r.get("text") or "").strip()
            if not finding or not _is_substantive(finding, min_chars=MIN_SUBSTANTIVE_CHARS_ALIGNMENT):
                continue
            if not user_text:
                user_text = "この会話から海山さんが気づいたことは?"
            yield {
                "user": user_text[:1000],
                "assistant": finding[:3000],
                "ts": (r.get("ts") or r.get("timestamp") or "")[:10],
                "user_id": f"clone_learning:{r.get('id', '?')}",
                "source": "clone_learning",
                "category": r.get("category", ""),
            }


def iter_clone_feedback_pairs() -> Iterator[dict]:
    """clone_feedback/*.jsonl から海山採用済 (= verdict=correct or accepted) を pair 化。

    各 record 想定 schema (= clone_feedback.py 経由):
      {
        "trigger_msg": "...",      # 社員質問
        "response": "...",          # bot 旧応答 (= 不正)
        "correction_msg": "...",    # 社員「違う」+ 訂正
        "verdict": "correct|wrong|...",
        "ts": "..."
      }

    correct (= 海山採用済の訂正) のみ。訂正後の正答を assistant に。
    """
    clone_feedback_dir = DATA_BRAIN / "clone_feedback"
    if not clone_feedback_dir.exists():
        return
    for f in sorted(clone_feedback_dir.glob("*.jsonl")):
        try:
            lines = f.read_text(encoding="utf-8").splitlines()
        except Exception:
            continue
        for line in lines:
            line = line.strip()
            if not line:
                continue
            try:
                r = json.loads(line)
            except Exception:
                continue
            if not isinstance(r, dict):
                continue
            if (r.get("verdict") or "").strip() not in ("correct", "accepted", "applied"):
                continue
            user_text = (r.get("trigger_msg") or r.get("user_query") or "").strip()
            correction = (
                r.get("corrected_response")
                or r.get("correction_msg")
                or r.get("correction")
                or ""
            ).strip()
            if not user_text or not correction:
                continue
            if not _is_substantive(correction, min_chars=MIN_SUBSTANTIVE_CHARS_ALIGNMENT):
                continue
            yield {
                "user": user_text[:1000],
                "assistant": correction[:3000],
                "ts": (r.get("ts") or r.get("timestamp") or "")[:10],
                "user_id": f"clone_feedback:{r.get('id', '?')}",
                "source": "clone_feedback",
            }


def iter_personal_line_conversations_pairs() -> Iterator[dict]:
    """raw/conversations/ から海山発言を抽出 (= 個人 LINE Bot との会話)。

    clone_history (= LW 社員 DM) とは別経路、海山個人 LINE bot とのやり取り。
    各 file の想定 schema: jsonl もしくは markdown。

    file 形式 (= 推測、本番側で要確認):
      jsonl: {"role": "user"|"assistant", "text": "...", "ts": "..."}
      md:    LW と同じ [HH:MM] <speaker>: <message> 形式

    海山発言 (= role=user) を assistant に、直前 bot 応答を user query に。
    (= 海山が bot にどう聞くか、どう反応するかの pattern)
    """
    conv_dir = DATA_BRAIN / "raw" / "conversations"
    if not conv_dir.exists():
        return
    for f in sorted(conv_dir.glob("*.jsonl")):
        try:
            lines = f.read_text(encoding="utf-8").splitlines()
        except Exception:
            continue
        records = []
        for line in lines:
            line = line.strip()
            if not line:
                continue
            try:
                r = json.loads(line)
            except Exception:
                continue
            if isinstance(r, dict):
                records.append(r)
        # role=user 発言を抽出 (= 海山個人 LINE Bot の user side、海山発言)
        for i, r in enumerate(records):
            if r.get("role") != "user":
                continue
            umiyama_msg = (r.get("text") or r.get("content") or "").strip()
            if not _is_substantive(umiyama_msg, min_chars=MIN_SUBSTANTIVE_CHARS_ALIGNMENT):
                continue
            # 直前 bot 応答 (= assistant)
            prev_bot = ""
            for j in range(i - 1, max(-1, i - 3), -1):
                if records[j].get("role") == "assistant":
                    prev_bot = (records[j].get("text") or records[j].get("content") or "").strip()
                    break
            if not prev_bot:
                prev_bot = f"(海山が個人 LINE Bot で発言開始)"
            # ★2026-05-23 Reviewer 指摘: 機密 keyword filter 適用
            if _text_has_sensitive(prev_bot) or _text_has_sensitive(umiyama_msg):
                continue
            yield {
                "user": prev_bot[:1500],
                "assistant": umiyama_msg[:3000],
                "ts": (r.get("ts") or r.get("timestamp") or "")[:10],
                "user_id": f"personal_line:{f.stem}",
                "source": "personal_line_conversations",
            }


# ─── source A-12/13/14: wiki/style/ 個人の話し方の癖 + 30問 / 135件 (★2026-05-23 海山指示) ─
def iter_response_bank_30q_pairs() -> Iterator[dict]:
    """wiki/style/response-bank.md の 30 問 + 「海山の実書き直し例」7 件を抽出。

    海山「話し方やコメントの仕方等のコミュニケーションStyle / 個人の癖を入れたい」を受けて。
    `iter_style_judgment_core_pairs` は ## 核となる主張 だけ抽出 = response-bank の 30 問
    Q&A 全体が漏れてた、これを救済。

    file 構造 (= response-bank.md):
      ### Q0-1: 「お疲れ様です。」
      **想定スケール**: XS
      > いつもお疲れさま。

      ### 例 1: 店長 / 売れ筋商品が薄い (Q: 売れ筋が入ってこない)
      > 商品の話は大事だよね。お店としては売れ筋を確保する必要がある。...
    """
    f = WIKI_DIR / "style" / "response-bank.md"
    if not f.exists():
        return
    try:
        content = f.read_text(encoding="utf-8")
    except Exception:
        return

    # ### Q0-1: 「お疲れ様です。」  ... > 回答  ... (次の ### or 終端まで)
    # ★fix 2026-05-23: 「」を optional に (= Q1〜Q30 は「」無し format で 30 件 leak していた)
    Q_RE = re.compile(
        r"###\s+(Q[\w\d-]+):\s*[「『]?(.+?)[」』]?\s*\n"    # ### Q0-1: 「...」 or ### Q1: ...
        r"(?:\*\*想定スケール\*\*:.*?\n)?"                  # **想定スケール**: XS (optional)
        r"\s*((?:>\s*.+\n?)+)",                              # > 回答 (1 行以上)
        re.MULTILINE,
    )
    for m in Q_RE.finditer(content):
        qid = m.group(1)
        question = m.group(2).strip()
        quote_block = m.group(3)
        # quote block から > を剥がす
        answer_lines = []
        for line in quote_block.splitlines():
            line = line.strip()
            if line.startswith(">"):
                answer_lines.append(line[1:].strip())
            elif not line:
                answer_lines.append("")
        answer = "\n".join(answer_lines).strip()
        if not _is_substantive(answer, min_chars=MIN_SUBSTANTIVE_CHARS_ALIGNMENT):
            continue
        yield {
            "user": question[:600],
            "assistant": answer[:1500],
            "ts": "2026-05-19",   # response-bank の last_updated
            "user_id": f"response_bank_30q:{qid}",
            "source": "response_bank_30q",
        }

    # ### 例 1: 店長 / 売れ筋商品が薄い (Q: 売れ筋が入ってこない)
    # > 商品の話は大事だよね。... (複数段落あり、★ で終わる場合あり)
    EX_RE = re.compile(
        r"###\s+例\s*(\d+):\s*(.+?)\s*\(Q:\s*(.+?)\)\s*\n"   # ### 例 N: タイトル (Q: 質問内容)
        r"((?:>\s*.*\n?)+)",                                  # quote block (1 行以上)
        re.MULTILINE,
    )
    for m in EX_RE.finditer(content):
        ex_num = m.group(1)
        category = m.group(2).strip()
        question = m.group(3).strip()
        quote_block = m.group(4)
        answer_lines = []
        for line in quote_block.splitlines():
            line = line.strip()
            if line.startswith(">"):
                answer_lines.append(line[1:].strip())
            elif not line:
                answer_lines.append("")
        answer = "\n".join(answer_lines).strip()
        if not _is_substantive(answer, min_chars=MIN_SUBSTANTIVE_CHARS_ALIGNMENT):
            continue
        yield {
            "user": f"[{category}] {question}"[:600],
            "assistant": answer[:2000],
            "ts": "2026-05-22",   # 書き直し例の追加日
            "user_id": f"response_bank_example:{ex_num}",
            "source": "response_bank_30q",  # = 同 source として扱う
            "category": category,
        }


def iter_response_bank_trial_135_pairs() -> Iterator[dict]:
    """wiki/style/response-bank-trial-2026-05.md の 135 件 海山書き直し済 Q&A を抽出。

    海山「個人の癖も入れたい」を受けて。alignment_trial run2 の AI 応答を海山が
    全件 review した書き直し版 → fine-tune の核に成る source。

    verdict 採用 (= "fix" or "ok") のみ、reject / ? は skip。

    file 構造:
      ### store-001 [TSA・在庫管理] ✏️ fix
      **質問**: 店長です。先月から売れ筋の商品があまり入ってきません。...
      **海山版応答**:
      > 商品の話は大事だよね。...

      ### store-020 [コラボフレーム] ❌ reject
      **質問**: ...
      **却下** (= この質問は AI 応答対象として不要、海山判断)
    """
    f = WIKI_DIR / "style" / "response-bank-trial-2026-05.md"
    if not f.exists():
        return
    try:
        content = f.read_text(encoding="utf-8")
    except Exception:
        return

    # ### {id} [{category}] {verdict_emoji}\n**質問**: ...\n**海山版応答**:\n> ...
    # ★fix 2026-05-23: re.DOTALL 削除 (= `.` が `\n` 含むと quote block が次 entry まで貪欲 match
    # して 134 件 leak していた)。DOTALL 無しで 1 行内 non-greedy + 改行で entry 区切り。
    ENTRY_RE = re.compile(
        r"###\s+([\w-]+)\s*\[(.+?)\]\s*(✏️\s*fix|❌\s*reject|❓\s*\?|✅\s*ok)\s*\n+"  # header
        r"\*\*質問\*\*:\s*(.+?)\n+"                                                    # question (1 行)
        r"\*\*海山版応答\*\*:\s*\n+"                                                  # 応答 marker
        r"((?:>\s*.*\n?)+)",                                                          # quote block
        re.MULTILINE,  # DOTALL 削除
    )
    for m in ENTRY_RE.finditer(content):
        qid = m.group(1)
        category = m.group(2).strip()
        verdict_raw = m.group(3)
        question = m.group(4).strip()
        quote_block = m.group(5)
        # verdict 判定
        if "reject" in verdict_raw or "?" in verdict_raw:
            continue  # reject / 保留 は skip
        # quote block から > を剥がす
        answer_lines = []
        for line in quote_block.splitlines():
            line = line.strip()
            if line.startswith(">"):
                answer_lines.append(line[1:].strip())
            elif not line:
                answer_lines.append("")
            else:
                break  # quote block 終端
        answer = "\n".join(answer_lines).strip()
        if not _is_substantive(answer, min_chars=20):
            continue
        if not _is_substantive(question, min_chars=10):
            continue
        verdict = "ok" if "ok" in verdict_raw else "fix"
        yield {
            "user": question[:800],
            "assistant": answer[:2500],
            "ts": "2026-05-22",
            "user_id": f"response_bank_trial:{qid}",
            "source": "response_bank_trial_135",
            "category": category,
            "verdict": verdict,
        }


def iter_style_detail_pairs() -> Iterator[dict]:
    """wiki/style/*.md の「## 核となる主張」 以外の detail section (= ルール / NG /
    なぜ / 文脈別) を取り込む。

    既存 iter_style_judgment_core_pairs は ## 核となる主張 のみ抽出 = style file の
    最も valuable な「具体ルール / NG パターン / 文脈別の選び方」が dataset 漏れてた。

    実装方針: 各 file から ## 核となる主張 section を除去した残りを、1 file 1 pair
    として yield。assistant 役に detail 本文、user 役に「{title} について、海山さんの
    具体的なルールや使い分けは?」を置く。
    """
    style_dir = WIKI_DIR / "style"
    if not style_dir.exists():
        return
    CORE_REMOVE_RE = re.compile(
        r"##\s*核となる主張[^\n]*\n.*?(?=\n##|\Z)", re.DOTALL
    )
    TITLE_RE = re.compile(r"^#\s+(.+?)$", re.M)

    # response-bank は別 iter で扱うので skip (= 二重抽出回避)
    SKIP_NAMES = {
        "response-bank.md",
        "response-bank-trial-2026-05.md",
        "_index.md",
        "index.md",
        "style-response-examples.md",  # 巨大 file、書き直し例 7 件は 30q iter で取る
    }

    for f in sorted(style_dir.glob("*.md")):
        if f.name in SKIP_NAMES or f.name.startswith("."):
            continue
        try:
            content = f.read_text(encoding="utf-8")
        except Exception:
            continue
        # frontmatter 除去
        if content.startswith("---\n"):
            parts = content.split("---\n", 2)
            if len(parts) >= 3:
                content = parts[2]
        # title 取得
        title_m = TITLE_RE.search(content)
        title = title_m.group(1).strip() if title_m else f.stem
        # ## 核となる主張 section 削除 (= 既存 iter_style_judgment_core_pairs で取得済)
        body = CORE_REMOVE_RE.sub("", content)
        # 1 行目 # title も削除 (= user 側に既出)
        body = re.sub(r"^#\s+.+?\n", "", body, count=1)
        body = body.strip()
        if not _is_substantive(body, min_chars=50):
            continue
        yield {
            "user": f"海山さんの「{title}」について、具体的なルールや使い分けは?"[:600],
            "assistant": body[:3000],
            "ts": "",
            "user_id": f"style_detail:{f.stem}",
            "source": "wiki_style_detail",
        }


# ─── source A-10/11: wiki/meetings/ plaud 議事録 (★2026-05-23 海山指示「機密だけ skip」) ─
# ★2026-05-23 Reviewer 指摘で _SENSITIVE_KEYWORDS_UNIFIED に統合 (DRY 違反 fix)
_MEETINGS_SECRET_KEYWORDS = _SENSITIVE_KEYWORDS_UNIFIED


def _meetings_is_confidential(text: str) -> bool:
    """text の中に機密 keyword が含まれるか (= _text_has_sensitive の旧名 alias、互換維持)。"""
    return _text_has_sensitive(text)


def iter_meetings_umiyama_quotes_pairs() -> Iterator[dict]:
    """wiki/meetings/*.md の `## 重要発言` section から **海山**: 「...」 quote 抽出。

    対象 file は wiki/meetings/ 直下のみ (= subdir quarantine/ は除外)。
    file 全体に機密 keyword 含む → 全 file skip。
    各 quote に機密 keyword 含む → 当該 quote skip。

    抽出 line 形式:
      - **海山**: 「<発言>」 — <context>
      - **海山丈司**: 「<発言>」
    (海山 or 海山丈司、context 部分は optional)

    Yields:
      pair {
        "user": "{meeting_title} で {context} について海山さんはどう発言したか?",
        "assistant": "<発言>",
        "source": "meeting_quote",
        ...
      }
    """
    if not MEETINGS_WIKI_DIR.exists():
        return
    QUOTE_RE = re.compile(
        r'-\s*\*\*海山(?:丈司)?\*\*\s*[:：]\s*「(.+?)」(?:\s*[—–\-]\s*(.+))?'
    )
    TITLE_RE = re.compile(r'^#\s+(.+?)(?:\s*[—–-]\s*\d{4}.*)?$', re.M)
    DATE_FM_RE = re.compile(r'^date:\s*(\d{4}-\d{2}-\d{2})', re.M)

    for f in sorted(MEETINGS_WIKI_DIR.glob("*.md")):
        try:
            content = f.read_text(encoding="utf-8")
        except Exception:
            continue
        # file 全体に機密 keyword 含むなら全 file skip (= 役員会 M&A 等)
        if _meetings_is_confidential(content):
            continue
        title_m = TITLE_RE.search(content)
        title = title_m.group(1).strip() if title_m else f.stem
        date_m = DATE_FM_RE.search(content)
        date_str = date_m.group(1) if date_m else (
            f.stem[:10] if re.match(r'\d{4}-\d{2}-\d{2}', f.stem) else ""
        )
        # ## 重要発言 section 取得
        section_m = re.search(
            r'##\s*重要発言\s*\n(.*?)(?=\n##|\Z)', content, re.DOTALL
        )
        if not section_m:
            continue
        section_text = section_m.group(1)
        for m in QUOTE_RE.finditer(section_text):
            quote = m.group(1).strip()
            context = (m.group(2) or "").strip()
            if not _is_substantive(quote, min_chars=MIN_SUBSTANTIVE_CHARS_ALIGNMENT):
                continue
            # quote 単体 / context 単体に機密 keyword あれば skip
            if _meetings_is_confidential(quote) or _meetings_is_confidential(context):
                continue
            if context:
                user_text = f"{title} の議論で、{context}について海山さんは何と言ったか?"
            else:
                user_text = f"{title} の議論で、海山さんは何と言ったか?"
            yield {
                "user": user_text[:600],
                "assistant": quote[:1500],
                "ts": date_str,
                "user_id": f"meeting_quote:{f.stem}",
                "source": "meeting_quote",
            }


def iter_meetings_judgments_pairs() -> Iterator[dict]:
    """wiki/meetings/*.md の `## 海山の判断軸 / レバレッジ点` section 全文を抽出。

    海山発言を蒸留した「判断軸」セクションは LLM が compile して残してる、
    本人視点の判断ロジック塊として高品質な assistant 応答素材。

    対象 file は wiki/meetings/ 直下のみ (= subdir quarantine/ 除外)。
    機密 keyword 含む section は skip。

    Yields:
      pair {
        "user": "{meeting_title} で海山さんはどう判断したか?",
        "assistant": "<section 全文>",
        "source": "meeting_judgment",
        ...
      }
    """
    if not MEETINGS_WIKI_DIR.exists():
        return
    TITLE_RE = re.compile(r'^#\s+(.+?)(?:\s*[—–-]\s*\d{4}.*)?$', re.M)
    DATE_FM_RE = re.compile(r'^date:\s*(\d{4}-\d{2}-\d{2})', re.M)
    SECTION_RE = re.compile(
        r'##\s*海山の判断軸[^\n]*\n(.*?)(?=\n##|\Z)', re.DOTALL
    )

    for f in sorted(MEETINGS_WIKI_DIR.glob("*.md")):
        try:
            content = f.read_text(encoding="utf-8")
        except Exception:
            continue
        if _meetings_is_confidential(content):
            continue
        title_m = TITLE_RE.search(content)
        title = title_m.group(1).strip() if title_m else f.stem
        date_m = DATE_FM_RE.search(content)
        date_str = date_m.group(1) if date_m else ""
        section_m = SECTION_RE.search(content)
        if not section_m:
            continue
        section_text = section_m.group(1).strip()
        if not _is_substantive(section_text, min_chars=20):
            continue
        if _meetings_is_confidential(section_text):
            continue
        yield {
            "user": f"{title} の議論で、海山さんはどう判断したか?"[:600],
            "assistant": section_text[:2000],
            "ts": date_str,
            "user_id": f"meeting_judgment:{f.stem}",
            "source": "meeting_judgment",
        }


# ─── source A-17: raw/notes/gdrive_* 業務資料 実 content (★2026-05-23 真因究明 fix) ─
def iter_raw_notes_gdrive_business_pairs() -> Iterator[dict]:
    """raw/notes/gdrive_{monday-dash-weekly,focus10,focus10-shared,monday-dash,wbr,area-managers}_*.md
    を全文 1 pair として dataset 化。

    海山「imported_drive 業務資料を取り込め」に対する fix:
    - 当初 wiki/imported_drive/*.pdf.md を読んでたが、これは drive stub のみで実 content 無し
    - 真の content は raw/notes/gdrive_<dir-name>_<file>.md (= file watcher が drive から download
      → content_extractor で PDF/xlsx → text 抽出 → raw/notes/ に保存) にある
    - debug endpoint /api/fine-tune/raw-structure-debug で確認:
      gdrive_monday-dash-weekly_* = 5 file、gdrive_focus10* = 4 file 実在

    file 名 prefix から category 抽出:
      gdrive_monday-dash-weekly_<filename>.md → category "monday-dash-weekly"
      gdrive_focus10_<filename>.md → "focus10"
      gdrive_focus10-shared_<filename>.md → "focus10-shared"
      gdrive_wbr_<filename>.md → "wbr"
      等
    """
    if not RAW_NOTES_DIR.exists():
        return

    # category prefix mapping (= raw/notes/gdrive_<category>_<rest>.md)
    BUSINESS_CATEGORIES = (
        "monday-dash-weekly",
        "monday-dash",
        "focus10-shared",  # longer first to avoid conflict with focus10
        "focus10",
        "wbr",
        "area-managers",
    )
    EXTRACT_FAIL_MARKER = "__extract_faile"  # PDF 抽出失敗 file の末尾 marker

    for f in sorted(RAW_NOTES_DIR.glob("gdrive_*.md")):
        # extract failed file は skip (= text 0、stub のみ)
        if EXTRACT_FAIL_MARKER in f.name:
            continue
        # category 判定 (= prefix 一致)
        category = None
        rest = None
        for cat in BUSINESS_CATEGORIES:
            prefix = f"gdrive_{cat}_"
            if f.name.startswith(prefix):
                category = cat
                rest = f.name[len(prefix):]
                break
        if not category:
            continue
        try:
            content = f.read_text(encoding="utf-8")
        except Exception:
            continue
        # 機密 keyword 全 file skip (= 役員人事 / M&A / 給与等)
        if _meetings_is_confidential(content):
            continue
        # frontmatter 除去 + title 抽出
        body = content
        if content.startswith("---\n"):
            parts = content.split("---\n", 2)
            if len(parts) >= 3:
                body = parts[2]
        title_m = re.search(r"^#\s+(.+?)$", body, re.MULTILINE)
        if title_m:
            title = title_m.group(1).strip()
        else:
            # rest (= file 名から ext 抜き) を title 代用
            title = rest.replace(".md", "").replace("_", " ").strip()
        body = body.strip()
        # 実質 content 必要 (= 200 chars 以上)
        if len(body) < 200:
            continue
        # 日付抽出
        date_m = re.search(r"(\d{8}|\d{4}-\d{2}-\d{2})", f.name)
        date_str = date_m.group(1) if date_m else ""
        if date_str and len(date_str) == 8 and date_str.isdigit():
            date_str = f"{date_str[:4]}-{date_str[4:6]}-{date_str[6:]}"
        src_key = category.replace("-", "_")
        yield {
            "user": f"OWNDAYS の {category} 資料「{title}」について教えて"[:600],
            "assistant": body[:4000],
            "ts": date_str,
            "user_id": f"raw_gdrive:{category}:{f.stem}",
            # ★2026-05-23 Reviewer 指摘 source 名衝突 fix: imported_drive_<cat> → raw_gdrive_<cat>
            # = A-16 (wiki stub) と A-17 (raw 実 content) を集計上区別可能に
            "source": f"raw_gdrive_{src_key}",
        }


# ─── source A-16: imported_drive/ 業務資料 (= monday-dash / focus10 / wbr 等) 全文取り込み (★2026-05-23 海山指示) ─
def iter_imported_drive_business_pairs() -> Iterator[dict]:
    """imported_drive/ の business 資料 (= 海山が日常 review してる KPI / 議事録 / 戦略文書) を
    全文 1 pair として dataset 化。

    海山指示「imported_drive/monday-dash-weekly / focus10 も内容としてはなんとかして取り込んで」
    を受けて。海山発言 vs スタッフ発言の識別困難 → 全文を「海山が見てる業務知識」として
    fine-tune model に記憶させる方針 (= 業績数字 / KPI 進捗 / 各部署状況の知識 base)。

    対象 subdir (= 海山が review してる業務資料群):
      - monday-dash-weekly/  (= 週次 Monday Dash 議事録 + 営業数値 + KPI 進捗 pdf)
      - monday-dash/         (= Monday Dash 周辺資料 / Leadership Survey / Weekly Oversea / 27 卒採用)
      - focus10/             (= focus10 子供支援本数原価 等)
      - focus10-shared/      (= focus10 差別化 / 新店 pipeline / 新店シミュレーション)
      - wbr/                 (= Weekly Business Review、Japan PM ドラフト等)
      - area-managers/       (= 担当店舗表)

    機密 keyword (= _MEETINGS_SECRET_KEYWORDS) 検出 → 全 file skip。
    stub (= drive bin 参照のみで実 content 無い .md) → skip。
    """
    base = WIKI_DIR / "imported_drive"
    if not base.exists():
        return

    BUSINESS_SUBDIRS = (
        "monday-dash-weekly",
        "monday-dash",
        "focus10",
        "focus10-shared",
        "wbr",
        "area-managers",
    )
    # Drive stub の特徴 marker (= 中身 thin、本物は別 file)
    STUB_MARKERS = (
        "バイナリファイル",
        "本体は file watcher",
        "中身検索: 取り込み後 vector search",
        "Google Drive 側で原本を参照",
    )
    TITLE_RE = re.compile(r"^#\s+(.+?)$", re.MULTILINE)
    DATE_FROM_NAME_RE = re.compile(r"(\d{8}|\d{4}-\d{2}-\d{2}|\d{4}/\d{2}/\d{2})")

    for sub in BUSINESS_SUBDIRS:
        d = base / sub
        if not d.exists():
            continue
        for f in sorted(d.glob("*.md")):
            try:
                content = f.read_text(encoding="utf-8")
            except Exception:
                continue
            # file 全体に機密 keyword → 全 file skip
            if _meetings_is_confidential(content):
                continue
            # frontmatter 除去 + title 抽出
            body = content
            if content.startswith("---\n"):
                parts = content.split("---\n", 2)
                if len(parts) >= 3:
                    body = parts[2]
            title_m = TITLE_RE.search(body)
            title = title_m.group(1).strip() if title_m else f.stem
            body = body.strip()
            # stub filter (= drive 参照のみで実 content 無い)
            if any(marker in body for marker in STUB_MARKERS):
                continue
            # 実質的 content 必要 (= 200 chars 以上 で stub じゃないと推定)
            if len(body) < 200:
                continue
            # 日付 (file 名から推定)
            date_m = DATE_FROM_NAME_RE.search(f.name)
            date_str = date_m.group(1) if date_m else ""
            # 8 桁を YYYY-MM-DD に変換
            if date_str and len(date_str) == 8 and date_str.isdigit():
                date_str = f"{date_str[:4]}-{date_str[4:6]}-{date_str[6:]}"
            # category 名 (= subdir をそのまま source 区分)
            src_key = sub.replace("-", "_")
            yield {
                "user": f"OWNDAYS の {sub} 資料「{title}」について教えて"[:600],
                "assistant": body[:4000],
                "ts": date_str,
                "user_id": f"imported_drive:{sub}:{f.stem}",
                "source": f"imported_drive_{src_key}",
            }


# ─── source A-15: raw/voice/plaud/ 生 transcript から Speaker N (= 海山) 発言抽出 (★2026-05-23 海山指示) ─
def iter_plaud_raw_speaker_pairs() -> Iterator[dict]:
    """raw/voice/plaud/<file>.transcript.md から海山 (= umiyama_speaker) 発言を抽出。

    wiki/meetings/*.md frontmatter から:
      - umiyama_speaker: Speaker N
      - evidence: raw/voice/plaud/<file>.transcript.md
    を取得 → raw transcript の Speaker N label 行を全行 yield。

    speaker label format は複数考慮 (= plaud transcript format 推測):
      "Speaker 1: ..." / "Speaker 1\t..." / "**Speaker 1**: ..." / "[Speaker 1] ..."
    のいずれか match した行から発言抽出。

    機密 keyword (= _MEETINGS_SECRET_KEYWORDS) 検出:
    - file 全体 (raw + wiki) のどちらかに含まれる → 全 file skip
    - 個別発言に含まれる → 当該発言 skip

    数百件規模の追加が想定 (= 24 transcript × 平均 5-10 utterance、機密 file skip 後で
    ~120-240 件)。
    """
    if not MEETINGS_WIKI_DIR.exists():
        return

    # ★2026-05-23 真因 fix: plaud transcript format は **Speaker N HH:MM:SS\n<本文>**
    # = speaker label と発話本文が **別の行**。私の旧 regex は 1 行内期待で永遠 mismatch。
    # 新方式: speaker label 行を sequential 検出、次の Speaker label まで全行を本文として蓄積。
    SPEAKER_LINE_RE = re.compile(r"^Speaker\s+(\d+)\s+\d{1,2}:\d{2}:\d{2}\s*$")
    FM_UMIYAMA_RE = re.compile(r"^umiyama_speaker:\s*Speaker\s+(\d+)", re.MULTILINE)
    FM_EVIDENCE_RE = re.compile(
        r"^\s*-\s*(raw/voice/plaud/[^\s\n]+)", re.MULTILINE
    )
    DATE_FM_RE = re.compile(r"^date:\s*(\d{4}-\d{2}-\d{2})", re.MULTILINE)
    TITLE_RE = re.compile(r"^#\s+(.+?)(?:\s*[—–-]\s*\d{4}.*)?$", re.MULTILINE)

    def _parse_speaker_utterances(raw_text: str, target_num: str) -> list[tuple[str, str]]:
        """transcript を sequential 走査、target speaker の発話 + 直前他 speaker excerpt のペアを返す。

        Returns: [(prev_other_excerpt, target_utterance), ...]
        prev_other_excerpt は user query 多様化用 (= Reviewer A-3 対応、同一 query 量産防止)。
        """
        out: list[tuple[str, str]] = []
        current_speaker: str | None = None
        current_text: list[str] = []
        prev_other_excerpt: str = ""  # 直前他 speaker 発話の頭 100 char

        def _flush():
            nonlocal prev_other_excerpt
            if not current_speaker or not current_text:
                return
            text = " ".join(current_text).strip()
            if current_speaker == target_num:
                out.append((prev_other_excerpt, text))
            else:
                # 他 speaker → excerpt 保存 (次の海山発言の context 用)
                prev_other_excerpt = text[:120]

        for line in raw_text.splitlines():
            stripped = line.strip()
            if not stripped:
                continue
            m = SPEAKER_LINE_RE.match(stripped)
            if m:
                _flush()
                current_speaker = m.group(1)
                current_text = []
            else:
                current_text.append(stripped)
        _flush()
        return out

    for meeting_f in sorted(MEETINGS_WIKI_DIR.glob("*.md")):
        try:
            content = meeting_f.read_text(encoding="utf-8")
        except Exception:
            continue
        if not content.startswith("---\n"):
            continue
        parts = content.split("---\n", 2)
        if len(parts) < 3:
            continue
        fm, body = parts[1], parts[2]

        spk_m = FM_UMIYAMA_RE.search(fm)
        ev_m = FM_EVIDENCE_RE.search(fm)
        if not spk_m or not ev_m:
            continue
        umi_num = spk_m.group(1)
        raw_rel = ev_m.group(1).strip()
        raw_full = DATA_BRAIN / raw_rel
        if not raw_full.exists():
            continue

        try:
            raw_text = raw_full.read_text(encoding="utf-8")
        except Exception:
            continue

        # 機密 keyword: wiki 全体 or raw 全体 のどちらかに含まれる → 全 file skip
        if _meetings_is_confidential(content) or _meetings_is_confidential(raw_text):
            continue

        title_m = TITLE_RE.search(body)
        title = title_m.group(1).strip() if title_m else meeting_f.stem
        date_m = DATE_FM_RE.search(fm)
        date_str = date_m.group(1) if date_m else ""

        # 海山発言 + 直前 context excerpt の pair を抽出
        utt_pairs = _parse_speaker_utterances(raw_text, umi_num)

        # ★2026-05-23 海山指示 (= 後出し改訂):「精度高そうなものだけ」「意味なさそうな会話そのものも
        # スタイル」を反映、utterance 単位 + 質 filter 強化 方式に再変更。
        # 旧 file 集約は「意味なさそう」(= 短文・雑談・余白) を排除しすぎて style 死ぬ問題あり。
        #
        # 質 filter (= 「精度高そう」判定基準):
        # - カタカナ比率 < 30% (= 誤認識 hot zone を狭く、旧 40% から強化)
        # - 文末完結 check (= 「。/?/!/ね/な/だね/かな/よ/から/ので/ます/です/てる/だよ」等で終わる
        #   = 文字起こし途中切断 / 助詞断片を除外)
        # - 助詞のみで始まる断片を除外 (= 「のは」「とか」「だから」「でも」のみで始まる 5 char 未満)
        # - **短文 (10 char 未満) でも文末完結なら keep** (= 「うん。」「なるほど。」「いいね。」OK、
        #   海山指示「意味なさそうな会話もスタイル」反映)
        # - 機密 keyword は当然 skip
        UTT_END_PATTERNS = re.compile(
            r"(?:[。.\?!？!]|ね|な|だね|かな|よ|から|ので|ます|です|てる|だよ|でしょ|思う|でしょう)$"
        )
        BAD_START_PATTERNS = re.compile(r"^(?:のは|とか|だから|でも|けど|あと|から)\b")

        for i, (prev_excerpt, utt) in enumerate(utt_pairs):
            if not utt:
                continue
            # 機密 keyword は skip (= filter 強化)
            if _meetings_is_confidential(utt) or _meetings_is_confidential(prev_excerpt):
                continue
            # カタカナ比率 30% 超 → 誤認識 hot zone、skip
            kana_chars = sum(1 for c in utt if "゠" <= c <= "ヿ")
            if len(utt) > 0 and kana_chars / len(utt) > 0.30:
                continue
            # 助詞断片で始まる短文を除外 (= 5 char 未満の壊れた utterance)
            if len(utt) < 5 and BAD_START_PATTERNS.match(utt):
                continue
            # 文末完結 check (= 文字起こし途中切断 / 助詞断片を除外、ただし「意味なさそう」は keep)
            if not UTT_END_PATTERNS.search(utt[-6:]):
                # 文末完結してない → 文字起こし破損 risk 高い、skip
                continue
            # ここまで来たら「精度高そう」+ 完結文 → keep
            # user query 多様化 (= 直前他者発言があれば「{excerpt} という流れで」を入れる)
            if prev_excerpt:
                user_q = f"{title} の会議で「{prev_excerpt}...」という流れで、海山さんはどう続けた?"
            else:
                user_q = f"{title} の会議で、海山さんは冒頭で何と発言した?"
            yield {
                "user": user_q[:600],
                "assistant": utt[:1500],
                "ts": date_str,
                "user_id": f"plaud_speaker:{meeting_f.stem}:utt{i}",
                "source": "plaud_speaker",
            }


# ─── source B: raw/notes/(lineworks_|line_lineworks_|line_)*.md から海山発言抽出 (★2026-05-23) ─
def iter_lineworks_umiyama_pairs() -> Iterator[dict]:
    """LW チャット file (= 社員間チャット混在) から海山発言だけ抽出。

    file 構造:
      [HH:MM] <speaker name>: <message>
      [HH:MM] <speaker>: <message>
      ...

    抽出ロジック:
    1. 各行 `[HH:MM] <speaker>: <msg>` を regex parse
    2. speaker が UMIYAMA_DISPLAY_NAMES に match → 海山発言
    3. 直前 1-2 行の他者発言を user query (= 連結)
    4. 連続する海山発言は assistant に連結 (= 1 turn の発話まとまり)
    5. 機密 keyword を含む turn は skip (M&A / 人事評価 / 給与 等)
    6. うみやまAI / other speaker は除外

    実 LW スクレイパー出力の speaker 例 (= local audit):
    - "海山丈司" (149 件) / "海山(Umiyama)丈司(Takeshi)" (formal)
    - "うみやまAI" (79 件、AI 自身、除外)
    - "other" (53 件、未識別、除外)
    """
    if not RAW_NOTES_DIR.exists():
        return
    ACCEPTED_PREFIXES = ("lineworks_", "line_lineworks_", "line_")
    EXCLUDED_PREFIXES = ("line_file_",)
    UMIYAMA_NAMES = {
        "海山丈司", "海山(Umiyama)丈司(Takeshi)",
        "海山(Umiyama)タケシ(Takeshi)", "海山タケシ",
    }
    BOT_OR_UNKNOWN = {"うみやまAI", "other", ""}
    # ★2026-05-23 Reviewer 指摘: LW 独自 SECRET_KEYWORDS は買収/M&A 抜け穴あり → 統一 list に統合
    SECRET_KEYWORDS = _SENSITIVE_KEYWORDS_UNIFIED
    LINE_RE = re.compile(r"^\[(\d{1,2}:\d{2})\]\s*([^:]+?):\s*(.+)$")

    for f in sorted(RAW_NOTES_DIR.glob("*.md")):
        if not any(f.name.startswith(p) for p in ACCEPTED_PREFIXES):
            continue
        if any(f.name.startswith(p) for p in EXCLUDED_PREFIXES):
            continue
        try:
            content = f.read_text(encoding="utf-8")
        except Exception:
            continue

        # frontmatter 除去
        if content.startswith("---\n"):
            parts = content.split("---\n", 2)
            if len(parts) >= 3:
                content = parts[2]

        # 各行を parse、speaker + message tuple のリストに
        messages = []  # list of (speaker, message)
        for line in content.splitlines():
            m = LINE_RE.match(line)
            if not m:
                continue
            speaker = m.group(2).strip()
            message = m.group(3).strip()
            if not speaker or not message:
                continue
            messages.append((speaker, message))

        if not messages:
            continue

        # turn 化: 連続する同じ speaker は 1 turn に連結
        turns = []
        for sp, msg in messages:
            if turns and turns[-1]["speaker"] == sp:
                turns[-1]["message"] += "\n" + msg
            else:
                turns.append({"speaker": sp, "message": msg})

        # 海山発言を pair 化: 直前他者発言を user query、海山発言を assistant
        for i, turn in enumerate(turns):
            if turn["speaker"] not in UMIYAMA_NAMES:
                continue
            umiyama_msg = turn["message"].strip()
            # 機密 keyword 検出
            if any(kw in umiyama_msg for kw in SECRET_KEYWORDS):
                continue
            # 直前他者発言 (= 1-3 turn) を user query 候補に
            user_parts = []
            for j in range(max(0, i - 3), i):
                t = turns[j]
                if t["speaker"] in BOT_OR_UNKNOWN or t["speaker"] in UMIYAMA_NAMES:
                    continue
                user_parts.append(f"{t['speaker']}: {t['message']}")
            user_text = "\n".join(user_parts).strip()
            if not user_text:
                # 海山が会話開始 → file 名 (= room 名) を文脈に
                user_text = f"({f.stem} の議論で発言)"
            # 海山発言が短いと filter で落ちる可能性、4 字緩和
            if not _is_substantive(umiyama_msg, min_chars=MIN_SUBSTANTIVE_CHARS_ALIGNMENT):
                continue
            # user query 中の機密 keyword も検出 (= 海山発言は OK でも文脈が機密ならskip)
            if any(kw in user_text for kw in SECRET_KEYWORDS):
                continue
            ts_match = re.search(r"(\d{4}-\d{2}-\d{2})", f.name)
            ts = ts_match.group(1) if ts_match else ""
            yield {
                "user": user_text[:2000],   # 文脈は 2000 字 cap
                "assistant": umiyama_msg[:3000],
                "ts": ts,
                "user_id": f"lineworks:{f.stem}",
                "source": "lineworks_umiyama",
                "room": f.stem,
            }


# ─── source 6: alignment/ 配下 JSON 群 (= 100 問 / 50 問 / clone qa / vmd 等、★2026-05-23 真因判明追加) ─
def iter_alignment_dir_pairs() -> Iterator[dict]:
    """data/brain/alignment/ 配下の Q&A JSON を全部取り込み。

    対象 file pattern (= dict.answers 形式):
      - alignment_answers_*.json     (= 100 問海山回答 v1)
      - alignment_100_*.json         (= 100 問 v2)
      - alignment_50_personal_*.json (= 個人 50 問)
      - align_clone_qa*_owndays_*.json (= うみやまAI Q&A 100 問)
      - align_org_*.json / align_president_*.json / align_store_dev_*.json (= 業務 alignment)
      - ops_philosophy_*.json        (= 店舗哲学)
      - vmd_*_axes_*.json            (= VMD 軸 alignment)
      - response_alignment_answers_*.json (= 応答 alignment)

    除外: questions_*.json (= 質問のみで回答無し)

    各 record の answer 抽出ロジック:
      - comment (free text) があれば最優先 (= 海山が書いた deliberate な回答)
      - free_text フィールドがあれば次優先
      - choice_text (= 選択肢) を fallback
      - skipped=true は除外
    """
    if not ALIGNMENT_DIR.exists():
        return

    EXCLUDED_NAMES = {"questions_100.json", "questions_50.json",
                      "interview_coverage.json", "plaud_custom_vocabulary.csv"}

    for f in sorted(ALIGNMENT_DIR.glob("*.json")):
        if f.name in EXCLUDED_NAMES:
            continue
        try:
            data = json.loads(f.read_text(encoding="utf-8"))
        except Exception:
            continue
        if not isinstance(data, dict):
            continue
        answers = data.get("answers", []) or data.get("entries", [])
        if not isinstance(answers, list) or not answers:
            continue
        for r in answers:
            if not isinstance(r, dict):
                continue
            if r.get("skipped"):
                continue
            question = (r.get("question") or r.get("q") or "").strip()
            # answer 候補 (= 優先順): comment / free_text / choice_text
            answer = (
                (r.get("comment") or "").strip()
                or (r.get("free_text") or "").strip()
                or (r.get("choice_text") or "").strip()
                or (r.get("answer") or "").strip()
                or (r.get("answer_summary") or "").strip()
            )
            if not question or not _is_substantive(answer, min_chars=MIN_SUBSTANTIVE_CHARS_ALIGNMENT):
                continue
            # ★2026-05-23 Reviewer 指摘: 機密 keyword filter 適用 (= alignment_dir も無防備だった)
            if _text_has_sensitive(question) or _text_has_sensitive(answer):
                continue
            # ts (= answered_at) を ISO date に整形
            ts = (r.get("answered_at") or r.get("date") or r.get("ts") or "")[:10]
            yield {
                "user": question,
                "assistant": answer,
                "ts": ts,
                "user_id": f"alignment_dir:{f.stem}",
                "source": f"alignment_dir:{f.stem.split('_')[0]}",  # alignment / align / vmd 等
                "category": r.get("category", ""),
                "file": f.name,
            }


# ─── source 7: wiki/identity.md + style.md + thinking.md (★海山指示 2026-05-23) ─────
def iter_identity_style_thinking_pairs() -> Iterator[dict]:
    """wiki/identity.md / style.md / thinking.md の compile 出力を pair 化。

    海山指示: 「identity-style-thinking compile も Dataset に必要」
    これらは LLM が wiki/raw を集約した「海山像のサマリ」、本人発言ではないが
    海山が手動で監修している (= identity_seed / 校正されてる) ため fine-tune 価値あり。

    各 file を 1 つの pair として扱う (= ## section ごとに分けるとノイズ多い)。
    file 名から category を決め、user query を「{category} について教えて」型で合成。
    """
    SUMMARY_FILES = {
        "identity": "海山さんの人物像 / アイデンティティ",
        "style": "海山さんの言語スタイル / 話し方",
        "thinking": "海山さんの思考パターン / 判断軸",
    }
    for stem, label in SUMMARY_FILES.items():
        f = WIKI_DIR / f"{stem}.md"
        if not f.exists():
            continue
        try:
            content = f.read_text(encoding="utf-8")
        except Exception:
            continue
        # frontmatter 除去
        if content.startswith("---\n"):
            parts = content.split("---\n", 2)
            if len(parts) >= 3:
                content = parts[2]
        body = content.strip()
        if not _is_substantive(body, min_chars=MIN_SUBSTANTIVE_CHARS_ALIGNMENT):
            continue
        # ★2026-05-23 Reviewer 指摘: identity/style/thinking compile に LW 機密が反映される可能性 → filter 必須
        if _text_has_sensitive(body):
            continue
        # 巨大 file は最初の 8000 字に truncate (= fine-tune token 上限)
        # ★2026-05-23 Reviewer 指摘: marker は assistant に入れず metadata に
        truncated_flag = False
        if len(body) > 8000:
            body = body[:8000]
            truncated_flag = True
        yield {
            "user": f"{label} を教えて。",
            "assistant": body,
            "ts": "",
            "user_id": f"wiki_summary:{stem}",
            "source": f"wiki_summary:{stem}",
            "category": stem,
            "truncated": truncated_flag,
        }


# ─── source 8: response_quality_judge スコア lookup ─────────────
def load_quality_index() -> dict:
    """response_quality_judge の jsonl を読んで {ts: judge_dict} の index 作る。"""
    index: dict[str, dict] = {}
    if not QUALITY_DIR.exists():
        return index
    for f in sorted(QUALITY_DIR.glob("*.jsonl")):
        try:
            lines = f.read_text(encoding="utf-8").splitlines()
        except Exception:
            continue
        for line in lines:
            line = line.strip()
            if not line:
                continue
            try:
                r = json.loads(line)
            except Exception:
                continue
            ts = r.get("ts", "")
            if ts and "judge" in r:
                index[ts] = r["judge"]
    return index


def _enrich_with_quality(pair: dict, q_index: dict) -> dict:
    """pair に quality score を付与。scored=False なら未採点。"""
    j = q_index.get(pair.get("ts", ""))
    if not j or not isinstance(j, dict):
        pair["scored"] = False
        pair["min_quality"] = None
        return pair
    pair["scored"] = True
    pair["ai_smell"] = j.get("ai_smell")
    pair["mirroring_fit"] = j.get("mirroring_fit")
    pair["length_appropriate"] = j.get("length_appropriate")
    scores = [pair["ai_smell"], pair["mirroring_fit"], pair["length_appropriate"]]
    valid_scores = [s for s in scores if isinstance(s, (int, float))]
    pair["min_quality"] = min(valid_scores) if valid_scores else None
    return pair


# ─── 集約 + フィルタ ─────────────
# ★2026-05-23 海山指示: clone_history / ai_chat_questions は除外 (= default off)
# - clone_history: LW 社員 DM、bot 応答含む、海山 verify 無し
# - ai_chat_questions: 役割逆 (= 海山質問)、instruction tuning 構造逸脱
# 復活が必要なら env で opt-in 可能。
INCLUDE_CLONE_HISTORY = os.getenv("FINE_TUNE_INCLUDE_CLONE_HISTORY", "false").lower() == "true"
INCLUDE_AI_CHAT_QUESTIONS = os.getenv("FINE_TUNE_INCLUDE_AI_CHAT_QUESTIONS", "false").lower() == "true"


def collect_pairs(include_unscored: bool, min_quality: int) -> list[dict]:
    q_index = load_quality_index()
    all_pairs: list[dict] = []

    # ★2026-05-23 海山指示: clone_history / ai_chat_questions を default 除外
    iters: list = []
    if INCLUDE_CLONE_HISTORY:
        iters.append(iter_history_pairs)
    iters.extend([
        iter_alignment_trial_pairs,
        iter_alignment_history_pairs,
        iter_interview_wiki_pairs,        # ★2026-05-23 Vapi 蒸留採用済
        iter_raw_notes_alignment_pairs,   # ★2026-05-23 raw/notes alignment / align メモ
        iter_alignment_dir_pairs,         # ★2026-05-23 ★最大の収穫 alignment/ JSON 群 (海山「極力残せ」)
        iter_identity_style_thinking_pairs,  # ★2026-05-23 海山指示 wiki compile 出力
        iter_hobbies_wiki_pairs,          # ★2026-05-23 A-1 wiki/hobbies/ 海山視点 (海山「極力残せ」)
        iter_style_judgment_core_pairs,   # ★2026-05-23 A-2 wiki/style + judgment 核となる主張
        iter_decisions_and_meta_pairs,    # ★2026-05-23 A-3 wiki/decisions + system_improvements + meta
        iter_raw_notes_deliberate_pairs,  # ★2026-05-23 A-4 raw/notes umiyama_/example_ 等 deliberate
        iter_lineworks_umiyama_pairs,     # ★2026-05-23 B LW 海山発言抽出 (= speaker filter + 機密 skip)
    ])
    if INCLUDE_AI_CHAT_QUESTIONS:
        iters.append(iter_ai_chat_questions_pairs)
    iters.extend([
        iter_clone_learning_pairs,        # ★2026-05-23 A-7 clone_learning verdict=accepted (= 海山採用済)
        iter_clone_feedback_pairs,        # ★2026-05-23 A-8 clone_feedback verdict=correct (= 海山訂正)
        iter_personal_line_conversations_pairs,  # ★2026-05-23 A-9 raw/conversations 個人 LINE Bot
        iter_meetings_umiyama_quotes_pairs,      # ★2026-05-23 A-10 plaud 議事録 ## 重要発言 海山 quote (海山「極力残せ」)
        iter_meetings_judgments_pairs,            # ★2026-05-23 A-11 plaud 議事録 ## 海山の判断軸 (海山「極力残せ」)
        iter_response_bank_30q_pairs,             # ★2026-05-23 A-12 response-bank 30 問 + 書き直し例 7 件
        iter_response_bank_trial_135_pairs,       # ★2026-05-23 A-13 response-bank-trial 135 件 (verdict fix/ok のみ)
        iter_style_detail_pairs,                  # ★2026-05-23 A-14 wiki/style/ 核以外 (= ルール / NG / 文脈別)
        iter_plaud_raw_speaker_pairs,             # ★2026-05-23 A-15 plaud 生 transcript Speaker N (= 海山) 発言抽出 (海山「精度高そうなものだけ」)
        iter_imported_drive_business_pairs,       # ★2026-05-23 A-16 imported_drive/monday-dash/focus10/wbr 等業務資料
        iter_raw_notes_gdrive_business_pairs,     # ★2026-05-23 A-17 raw/notes/gdrive_* 実 content (= stub じゃない本物)
    ])

    for fn in iters:
        for p in fn():
            p = _enrich_with_quality(p, q_index)
            all_pairs.append(p)

    # フィルタ
    # ★2026-05-23: 高品質 source (= 海山採用 / 直接回答) は採点なしでも常に採用
    HIGH_QUALITY_SOURCE_PREFIXES = (
        "alignment_",       # alignment_history / alignment_trial:* (= 海山直答 / 採用)
        "wiki_interview",   # Vapi 蒸留採用済 (= /align-voice-accept 経由)
        "raw_notes",        # 海山 deliberate メモ (= /teach /forward 直書き)
        "alignment_dir",    # alignment/ JSON 群 ~700 件
        "wiki_summary",     # wiki/identity / style / thinking
        "wiki_hobbies",     # ★2026-05-23 wiki/hobbies 海山視点 section
        "wiki_style_core",  # ★2026-05-23 wiki/style 核となる主張
        "wiki_judgment_core", # ★2026-05-23 wiki/judgment 核となる主張
        "wiki_decisions",   # ★2026-05-23 wiki/decisions
        "system_improvements", # ★2026-05-23 海山判断 改善 record
        "meta_alignment",   # ★2026-05-23 月次 alignment snapshot
        "lineworks_umiyama", # ★2026-05-23 LW 海山発言抽出 (機密 skip + speaker filter)
        "ai_chat_questions", # ★2026-05-23 A-6 chatgpt_/claude_ 海山質問パターン (= 海山 deliberate)
        "clone_learning",    # ★2026-05-23 A-7 clone_learning 海山採用済 finding
        "clone_feedback",    # ★2026-05-23 A-8 clone_feedback 海山訂正 (= correct verdict)
        "personal_line_conversations", # ★2026-05-23 A-9 raw/conversations 個人 LINE Bot 海山発言
        "meeting_quote",      # ★2026-05-23 A-10 plaud 議事録 ## 重要発言 海山 quote (機密 skip)
        "meeting_judgment",   # ★2026-05-23 A-11 plaud 議事録 ## 海山の判断軸 セクション (機密 skip)
        "response_bank_30q",  # ★2026-05-23 A-12 response-bank 30 問 + 書き直し例 7 件 (= 海山本人記入)
        "response_bank_trial_135",  # ★2026-05-23 A-13 alignment_trial 135 件 海山書き直し済 (fix/ok)
        "wiki_style_detail",  # ★2026-05-23 A-14 wiki/style/ 核以外 detail (= 具体ルール / NG / 文脈別)
        "plaud_speaker",      # ★2026-05-23 A-15 plaud 生 transcript Speaker N 海山発言 (機密 skip + 量重視)
        "imported_drive_",    # ★2026-05-23 A-16 wiki/imported_drive/ stub fallback (機密 skip 済)
        "raw_gdrive_",        # ★2026-05-23 A-17 raw/notes/gdrive_* 実 content (機密 skip 済、本命)
    )
    filtered = []
    for p in all_pairs:
        if any(p["source"].startswith(prefix) for prefix in HIGH_QUALITY_SOURCE_PREFIXES):
            filtered.append(p)
            continue
        if p["scored"]:
            if p["min_quality"] is not None and p["min_quality"] >= min_quality:
                filtered.append(p)
        elif include_unscored:
            # 採点無しでも採用 (= dataset 量を確保したい時)
            filtered.append(p)

    # ★2026-05-23 Reviewer 指摘: global dedupe (= identity/style/thinking と core/detail 重複、
    # 30q 例 と trial 135 で同 content 等が dataset に重複 yield されてた)
    seen_hashes: set[str] = set()
    dedup_kept: list[dict] = []
    for p in filtered:
        # assistant 先頭 500 char で MD5 hash (= 軽微差は重複扱い、末尾差は無視)
        content_key = (p.get("assistant") or "")[:500].strip()
        if not content_key:
            dedup_kept.append(p)
            continue
        h = hashlib.md5(content_key.encode("utf-8")).hexdigest()
        if h in seen_hashes:
            continue
        seen_hashes.add(h)
        dedup_kept.append(p)
    n_dedup = len(filtered) - len(dedup_kept)
    if n_dedup > 0:
        logger.info(f"global dedupe removed {n_dedup} duplicate pairs (= MD5 of assistant[:500])")

    return dedup_kept


# ─── OpenAI fine-tune 形式に変換 ─────────────
def to_openai_format(pair: dict) -> dict:
    return {
        "messages": [
            {"role": "system", "content": SYSTEM_PROMPT_FOR_TUNING},
            {"role": "user", "content": pair["user"][:4000]},
            {"role": "assistant", "content": pair["assistant"][:4000]},
        ],
        "metadata": {
            "source": pair["source"],
            "ts": pair.get("ts", ""),
            "min_quality": pair.get("min_quality"),
            "user_id_short": (pair.get("user_id") or "")[:16],
        },
    }


# ─── 集計レポート ─────────────
def build_report(pairs: list[dict], include_unscored: bool, min_quality: int) -> str:
    n = len(pairs)
    by_source: Counter = Counter()
    by_quality: Counter = Counter()
    user_lens = []
    bot_lens = []
    for p in pairs:
        # source の category だけ取る (= alignment_trial:run_id:verdict → alignment_trial)
        src_cat = p["source"].split(":")[0]
        by_source[src_cat] += 1
        q = p.get("min_quality")
        if q is None:
            by_quality["unscored"] += 1
        else:
            by_quality[str(int(q))] += 1
        user_lens.append(len(p["user"]))
        bot_lens.append(len(p["assistant"]))

    def _stats(lst):
        if not lst:
            return {"min": 0, "median": 0, "mean": 0, "max": 0}
        srt = sorted(lst)
        return {
            "min": srt[0],
            "median": srt[len(srt) // 2],
            "mean": int(sum(srt) / len(srt)),
            "max": srt[-1],
        }

    user_s = _stats(user_lens)
    bot_s = _stats(bot_lens)

    lines = [
        f"# fine-tune dataset v1 集計レポート",
        f"",
        f"- 生成日時: {datetime.now(JST).isoformat(timespec='seconds')}",
        f"- フィルタ: min_quality={min_quality}, include_unscored={include_unscored}",
        f"",
        f"## 採用件数",
        f"- **合計: {n} 件**",
        f"",
        f"## source 別内訳",
    ]
    for src, c in by_source.most_common():
        lines.append(f"- {src}: {c} 件 ({c / max(1, n) * 100:.1f}%)")
    lines.append("")
    lines.append(f"## quality score (min 軸) 分布")
    for q in ["5", "4", "3", "2", "1", "unscored"]:
        c = by_quality.get(q, 0)
        if c > 0:
            lines.append(f"- {q}: {c} 件 ({c / max(1, n) * 100:.1f}%)")
    lines.append("")
    lines.append(f"## 文字数分布")
    lines.append(f"- user query:  min={user_s['min']} / median={user_s['median']} / mean={user_s['mean']} / max={user_s['max']}")
    lines.append(f"- bot response: min={bot_s['min']} / median={bot_s['median']} / mean={bot_s['mean']} / max={bot_s['max']}")
    lines.append("")
    lines.append(f"## 評価 (= fine-tune 着手可能性)")
    if n < 100:
        lines.append(f"- ⚠️ {n} 件は fine-tune には少なすぎ (= 推奨 500+、最低 100)")
        lines.append(f"- → --include-unscored 試す or response_quality_judge cron が貯まるのを待つ")
    elif n < 500:
        lines.append(f"- ⚠️ {n} 件は最低限、fine-tune 初動には足りるが少なめ")
        lines.append(f"- → continue training / 2nd epoch で底上げ要")
    else:
        lines.append(f"- ✅ {n} 件あれば GPT-4o-mini fine-tune に十分")
        lines.append(f"- 推奨: 70% (train) / 15% (val) / 15% (test) 分割")
    return "\n".join(lines)


# ─── main ─────────────
def main():
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--version", default="v1", help="出力 dataset version (default v1)")
    parser.add_argument("--report", action="store_true", help="集計レポートのみ表示、jsonl 出力なし")
    parser.add_argument("--include-unscored", action="store_true",
                        help="quality 未採点 turn も含める (= dataset 量を増やしたい時)")
    parser.add_argument("--min-quality", type=int, default=3,
                        help="quality min 閾値 (default 3、4 = 海山品質、5 = 最高のみ)")
    args = parser.parse_args()

    OUT_DIR.mkdir(parents=True, exist_ok=True)

    logger.info(f"collecting pairs (min_quality={args.min_quality}, include_unscored={args.include_unscored})")
    pairs = collect_pairs(args.include_unscored, args.min_quality)
    logger.info(f"collected {len(pairs)} pairs")

    report = build_report(pairs, args.include_unscored, args.min_quality)
    print(report)
    print()

    if args.report:
        return 0

    # jsonl 出力 (OpenAI fine-tune 形式)
    dataset_path = OUT_DIR / f"dataset_{args.version}.jsonl"
    report_path = OUT_DIR / f"report_{args.version}.md"
    with dataset_path.open("w", encoding="utf-8") as f:
        for p in pairs:
            f.write(json.dumps(to_openai_format(p), ensure_ascii=False) + "\n")
    report_path.write_text(report, encoding="utf-8")

    logger.info(f"wrote {len(pairs)} records to {dataset_path}")
    logger.info(f"wrote report to {report_path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
