#!/usr/bin/env python3
"""scripts/magazine_persona_ingest.py — OWNDAYS MAGAZINE「海山社長のもぐもぐダイアリー」を人格へ取込。

★2026-07-05 海山指示「いままで書いた OWNDAYS magazine の自分のパートは人格補完に使える。
全てを読んで抽出して」。

もぐもぐダイアリー = 海山本人が一人称で書く連載コラム (= magazine 内で唯一の「自分のパート」。
他セクションは社員が書いた第三者記事)。本人の文体・ユーモア・価値観・自己認識が濃く出る一次資料。

二層 (life-story / 音声 alignment と同じ思想):
  ① 原文保全 → raw/magazine_diary/<id>.md (frontmatter だけ無害化・provenance)
  ② 蒸留 → alignment_interview.extract_session (interview_extracted/ に **レビュー待ち**、
     採用は海山、捏造ゲート継承)。

★2026-07-05 cross-check (Reviewer/DA) 反映:
  - 専用 prompt (ai.MAGAZINE_EXTRACT_PROMPT): ★海山確認「本人が全部執筆、大半は本音で嘘なし。
    一部だけ士気を上げる他所行きの文章」→ 文体/ユーモア/内省も本人由来として取り込むが、
    建前・演出・レトリック反転 (「嘘である」の否定) は本心と取り違えず割り引く。書き言葉の癖は
    話し方と混同しない旨を明記。生々しい私的深層は【保存しない】ルールに委ねる。
  - credit_coverage=False + record_session を呼ばない: 音声 coverage (通話回数/depth) を汚さない
    (文章由来で薄い次元が「埋まった」ことにして肉声の深掘りを starve させない、DA #2)。
  - source="magazine" タグ + --limit (既定 15): 全巻投入時のレビュー洪水→一括承認でゲート
    無力化を防ぐ (少しずつ trickle)。

diary は対話でなく一人称エッセイなので、life_story.build_transcript で「全文が海山の言葉」と
明示して渡す (話者帰属制約と整合 = AI 発話由来の癖混入を構造的に排除)。

入力: raw/notes/ の onmaga_batch_*.md (本文入り) / mogumog_*.md (scraper 修正後は本文入り)。
  onmaga_batch は 1 file に複数号が連結、各号の目次(■一覧)と本文の両方に marker が出るため、
  「marker〜次の■」の最長 segment = 本文を採る (scraper の JS 修正と同ロジック)。

実行 (Mac Studio / container、LITELLM 要):
  python3 scripts/magazine_persona_ingest.py --dry-run          # 抽出されるコラムを確認
  python3 scripts/magazine_persona_ingest.py                    # 蒸留 → レビュー待ち
状態は data/brain/.magazine_ingest_state.json で再取込防止 (magazine id 単位)。
"""
from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))         # scripts/ sibling
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))     # repo root

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger("magazine_persona_ingest")

ROOT = Path(__file__).resolve().parents[1]
BRAIN_ROOT = Path(os.getenv("BRAIN_ROOT", ROOT / "data" / "brain"))
NOTES_DIR = BRAIN_ROOT / "raw" / "notes"
DIARY_RAW_DIR = BRAIN_ROOT / "raw" / "magazine_diary"
STATE_FILE = BRAIN_ROOT / ".magazine_ingest_state.json"
LITELLM_URL = os.getenv("LITELLM_URL", "http://localhost:4000")
LITELLM_KEY = os.getenv("LITELLM_KEY") or os.getenv("LITELLM_MASTER_KEY", "")

DIARY_MARKER = "海山タケシ社長のもぐもぐダイアリー"
# marker 直後に来がちな byline (本文でない名乗り行)。先頭から剥がす。
# ★2026-07-05 Reviewer R2: 「短い行を無差別に剥がす」のをやめ、既知の byline パターン
# (社長 / 海山〜 の名乗り、役職括弧) だけに限定 — 短い punchy な書き出しを誤って落とさない。
_BYLINE_RE = re.compile(r"^(社長|海山[^。]{0,8}|.{0,10}（.*）|.{0,10}\(.*\))$")
# 本文と判定する最小長 (目次断片 ~20字を確実に弾き、実コラム ~500字+ は必ず通す)
MIN_BODY_CHARS = 120
_ID_RE = re.compile(r"owndays-magazine-details/(\d+)")
_VOL_RE = re.compile(r"Vol\.?\s*(\d+)", re.IGNORECASE)


def _isolate_body(segment: str) -> str:
    """marker〜次の■ の segment から、byline 行を剥がして本文だけ返す。
    byline は既知パターンのみ剥がす (Reviewer R2: 過剰除去を避ける)。"""
    lines = segment.splitlines()
    while lines:
        s = lines[0].strip()
        if s == "" or _BYLINE_RE.match(s):
            lines.pop(0)
            continue
        break
    return "\n".join(lines).strip()


def longest_diary_segment(text: str) -> str:
    """text 内の全 marker 出現について「marker〜次の■」を切り出し、最長=本文を返す。
    目次(■一覧)の marker は次の項目まで極短 → 自然に本文に負ける (scraper JS と同ロジック)。"""
    best = ""
    start = 0
    while True:
        idx = text.find(DIARY_MARKER, start)
        if idx < 0:
            break
        seg = text[idx + len(DIARY_MARKER):].split("■", 1)[0]
        if len(seg) > len(best):
            best = seg
        start = idx + len(DIARY_MARKER)
    return _isolate_body(best)


_HEADER_LOOKBACK = 200   # URL の直前にある「## Vol.XXX」ヘッダを取り込む窓


def _split_issues(text: str) -> list[dict]:
    """onmaga_batch (複数号連結) を magazine id 単位に分割。
    'URL: .../owndays-magazine-details/<id>' の出現位置で区切る。各号の slice は
    **その URL の少し手前 (ヘッダ「## Vol.XXX」を含む窓) から次の URL 直前まで**。
    ★2026-07-05 Reviewer R1: 2号目以降が直前ヘッダを取りこぼし Vol 番号が空になる問題を、
    URL 手前 _HEADER_LOOKBACK 字を各号に含めることで解消 (body 領域は URL 以降なので重複無害)。
    id 無し (単体 mogumog) は 1 塊。"""
    matches = list(_ID_RE.finditer(text))
    if not matches:
        return [{"id": "", "text": text}]
    issues = []
    for i, m in enumerate(matches):
        start = max(0, m.start() - _HEADER_LOOKBACK)
        next_start = matches[i + 1].start() if i + 1 < len(matches) else len(text)
        issues.append({"id": m.group(1), "text": text[start:next_start]})
    return issues


def extract_diary_columns(text: str, src_name: str = "") -> list[dict]:
    """1 file のテキストから 海山 diary コラムを号単位で抽出。
    返り値: [{id, vol, body, src}] (本文が MIN_BODY_CHARS 以上のもののみ)。"""
    out = []
    for issue in _split_issues(text):
        body = longest_diary_segment(issue["text"])
        if len(body) < MIN_BODY_CHARS:
            continue
        vm = _VOL_RE.search(issue["text"])
        out.append({
            "id": issue["id"] or (src_name.rsplit(".", 1)[0]),
            "vol": (vm.group(1) if vm else ""),
            "body": body,
            "src": src_name,
        })
    return out


def collect_all_columns(notes_dir: Path = NOTES_DIR) -> list[dict]:
    """raw/notes/ の onmaga_batch_*.md + mogumog_*.md から全 diary コラムを抽出 + id 重複除去
    (同 id は本文が長い方=情報量の多い方を残す。batch と mogumog の二重取込防止)。"""
    by_id: dict[str, dict] = {}
    # ★2026-07-05 Reviewer R3: scraper は mogumog を .txt で書く → .md/.txt 両対応。
    files = (sorted(notes_dir.glob("onmaga_batch_*.md"))
             + sorted(notes_dir.glob("onmaga_batch_*.txt"))
             + sorted(notes_dir.glob("mogumog_*.md"))
             + sorted(notes_dir.glob("mogumog_*.txt")))
    for f in files:
        try:
            text = f.read_text(encoding="utf-8")
        except Exception as e:
            logger.warning(f"read 失敗 {f.name}: {e}")
            continue
        for col in extract_diary_columns(text, src_name=f.name):
            cur = by_id.get(col["id"])
            if cur is None or len(col["body"]) > len(cur["body"]):
                by_id[col["id"]] = col
    return sorted(by_id.values(), key=lambda c: c["id"])


def _load_state() -> set:
    if STATE_FILE.exists():
        try:
            return set(json.loads(STATE_FILE.read_text(encoding="utf-8")).get("done_ids", []))
        except Exception:
            pass
    return set()


def _save_state(done: set) -> None:
    STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
    STATE_FILE.write_text(
        json.dumps({"done_ids": sorted(done)}, ensure_ascii=False, indent=2),
        encoding="utf-8")


def _preserve_raw(col: dict) -> Path:
    """原文を raw/magazine_diary/<id>.md に保存 (provenance、一次資料)。
    ★2026-07-05 Reviewer: body 内の行頭 `---` / `clone_visibility:` は life_story と同じく
    無害化 (将来 raw/ を rglob する reader への frontmatter injection 対策、原文主義は維持)。"""
    from services.life_story import sanitize_chapter
    DIARY_RAW_DIR.mkdir(parents=True, exist_ok=True)
    p = DIARY_RAW_DIR / f"{col['id']}.md"
    p.write_text(
        f"---\ntype: magazine_diary\nsource: onmaga\n"
        f"magazine_id: {col['id']}\nvol: {col['vol']}\n"
        f"clone_visibility: private\n---\n"
        f"# もぐもぐダイアリー Vol.{col['vol']} (id {col['id']})\n\n"
        f"{sanitize_chapter(col['body'])}\n",
        encoding="utf-8")
    return p


# ★2026-07-05 magazine cross-check DA #3: 「全て」= 全巻(300+号)を一度に流すと
# レビュー待ちが数百件に膨れ、人が捌けず「一括承認」でゲートが無力化する。既定で
# 1 実行あたりの蒸留数を上限化し、cron/手動で少しずつ trickle させる (--limit 0 で無制限)。
DEFAULT_LIMIT = 15


async def run(dry_run: bool = False, notes_dir: Path = NOTES_DIR,
              limit: int = DEFAULT_LIMIT) -> dict:
    columns = collect_all_columns(notes_dir)
    done = _load_state()
    todo = [c for c in columns if c["id"] not in done]
    logger.info(
        f"diary コラム 全 {len(columns)} 件 / 未取込 {len(todo)} 件 "
        f"(source: {notes_dir})")

    if dry_run:   # LITELLM/httpx 不要 (Reviewer minor: 遅延 import)
        for c in columns:
            mark = "✓済" if c["id"] in done else "→未"
            preview = c["body"][:180].replace("\n", " ")
            print(f"\n=== Vol.{c['vol']} (id {c['id']}, {len(c['body'])}字) {mark} [{c['src']}] ===\n{preview}…")
        return {"ok": True, "total": len(columns), "pending": len(todo), "dry_run": True}

    if not todo:
        # pending_todo = 「未取込が本当に 0」の明示 (★2026-07-06 workflow レビュー BLOCKER:
        # remaining キー欠落を caller が get(...,0) で読むと「取込むものが無い」と
        # 「全号取込済」を区別できず偽完了する)
        return {"ok": True, "total": len(columns), "extracted": 0, "failed": 0,
                "pending_todo": 0, "remaining": 0,
                "note": "全 diary 取込済 (pending 0)"}

    batch = todo if not limit else todo[:limit]
    capped = len(todo) - len(batch)

    import httpx
    import alignment_interview as ai                  # 遅延 import (蒸留時のみ、LITELLM 環境)
    from services.life_story import build_transcript

    extracted = 0
    failed = 0
    async with httpx.AsyncClient(timeout=180.0) as http:
        for c in batch:
            title = f"OWNDAYS MAGAZINE もぐもぐダイアリー Vol.{c['vol']}"
            # 全文が海山名義の公開コラム = 一人称整形 (話者帰属整合)。ただし蒸留側は
            # MAGAZINE_EXTRACT_PROMPT で「公開文=観察系のみ・私的深層/文体禁止・medium 上限」に、
            # かつ credit_coverage=False で音声 coverage を汚さない (DA #1/#2)。
            transcript = build_transcript(title, c["body"])
            try:
                _preserve_raw(c)                                   # ① 原文保全
                res = await ai.extract_session(                    # ② 蒸留 (レビュー待ち)
                    transcript, http, LITELLM_URL, LITELLM_KEY,
                    raw_filename=f"magazine-{c['id']}",
                    prompt_template=ai.MAGAZINE_EXTRACT_PROMPT,
                    credit_coverage=False, source="magazine")
                # ★2026-07-06 workflow レビュー MAJOR: extract_session は LLM 全滅時に
                # raise せず {"error": ...} を返す (interview_extracted への保存も無し)。
                # これを done 扱いすると LITELLM 断の cron 時間帯に未蒸留の号が
                # done_ids へ恒久登録され、復旧に state 手術が要る → failed 計上して retry 可能に
                if isinstance(res, dict) and res.get("error"):
                    failed += 1
                    logger.warning(f"  蒸留失敗 (LLM) Vol.{c['vol']} (id {c['id']}): {str(res.get('error'))[:120]}")
                    continue
                logger.info(f"  蒸留→レビュー待ち: Vol.{c['vol']} (id {c['id']})")
                extracted += 1
                done.add(c["id"])
            except Exception as e:
                failed += 1
                logger.warning(f"  抽出失敗 Vol.{c['vol']} (id {c['id']}): {type(e).__name__}: {e}")
    if extracted:
        _save_state(done)
    note = "採用は interview_extracted のレビュー (/align-voice) → apply_extraction で海山が実施"
    if capped:
        note += f" / 残り {capped} 件は今回上限(--limit {limit})でskip、再実行で継続"
    return {"ok": True, "total": len(columns), "extracted": extracted,
            "failed": failed, "pending_todo": len(todo) - extracted,
            "remaining": capped, "note": note}


def main() -> int:
    ap = argparse.ArgumentParser(
        description="OWNDAYS MAGAZINE もぐもぐダイアリー(海山パート)を人格へ蒸留(レビュー待ち)")
    ap.add_argument("--dry-run", action="store_true", help="抽出される diary コラムを表示(蒸留しない)")
    ap.add_argument("--notes-dir", default=str(NOTES_DIR), help="raw/notes/ のパス(既定=本番)")
    ap.add_argument("--limit", type=int, default=DEFAULT_LIMIT,
                    help=f"1 実行の蒸留上限 (既定 {DEFAULT_LIMIT}、0=無制限)。洪水→一括承認の防止")
    a = ap.parse_args()
    r = asyncio.run(run(dry_run=a.dry_run, notes_dir=Path(a.notes_dir), limit=a.limit))
    print(json.dumps(r, ensure_ascii=False))
    return 0 if r.get("ok") else 1


if __name__ == "__main__":
    raise SystemExit(main())
