"""
clone_memory_privacy_review.py — clone_memory の Privacy 再評価 (項目 10)

Personal.ai の "data ownership" / Letta の sleep-time pattern にインスパイア。
既存 clone_memory/<user_id>.md を nightly で **再走査** し、
「今読み直すと private にすべき」シグナルを検出 → 該当行を archive (削除) する。

設計:
  - 既存 memory は応答直後 (fast-gpt) と idle 30 秒 (smart sleep-time) で更新済
  - だが「PrivacyGate を通った後の memory」も後から見直すと private 判定すべきもの
    がある可能性 (時の経過で機密度が変わる / 個人特定リスクが見えてきた等)
  - LLM (smart) で 6 観点で再評価:
    1. 個人特定情報 (氏名 / 部署 / 役職 + 私的情報の組合せ)
    2. 健康深刻情報 (病名 / 治療 / 精神状態 詳細)
    3. 家族プライベート (配偶者・子供の個別事情)
    4. 第三者の評価・悪口 (社員 / 取引先個人攻撃)
    5. 進行中 M&A / 機密案件
    6. 性的内容
  - private 判定された行は memory から削除、archive log に記録

cron: 毎日 04:00 JST (auto_improve 03:00 / regression 03:30 の後)
  bash scripts/clone_cron.sh privacy-review

出力:
  data/brain/clone_improve/privacy_review/YYYY-MM-DD.json (再評価結果サマリ)
  data/brain/clone_improve/privacy_review/archived/<user>-<date>.md (削除された行の archive)
  data/brain/clone_improve/privacy_review.log.jsonl

実行:
  python3 scripts/clone_memory_privacy_review.py
  python3 scripts/clone_memory_privacy_review.py --dry-run
  python3 scripts/clone_memory_privacy_review.py --user u1 (1 user だけ)
"""
from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
import re
import sys
from datetime import datetime
from pathlib import Path
from typing import Optional

sys.path.insert(0, str(Path(__file__).resolve().parent))
from clone_improve_lib import call_llm, line_push, append_jsonl, ensure_dirs, JST

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger("clone_memory_privacy_review")

BRAIN_ROOT = Path(os.getenv("BRAIN_ROOT", "/app/data/brain"))
MEMORY_DIR = BRAIN_ROOT / "clone_memory"
REVIEW_DIR = BRAIN_ROOT / "clone_improve" / "privacy_review"
ARCHIVE_DIR = REVIEW_DIR / "archived"
LOG_PATH = BRAIN_ROOT / "clone_improve" / "privacy_review.log.jsonl"

# 1 回の cron で再評価する user 数 (毎日全 user は重い、ローテーション)
DAILY_BATCH_SIZE = int(os.getenv("PRIVACY_REVIEW_BATCH", "10"))


PRIVACY_REVIEW_PROMPT = """あなたは privacy review エージェント。
うみやまAI の clone_memory ファイル (社員 1 名分) を再走査し、
「今読み直すと private 化すべき」行を検出します。

【7 観点で評価 (★CLAUDE.md §1.9 と整合)】
1. 個人特定情報 (氏名 / 部署 / 役職 + 私的情報の組合せで個人特定可能)
2. 健康深刻情報 (病名 / 治療 / 精神状態の詳細)
3. 家族プライベート (配偶者・子供の個別事情)
4. 第三者の評価・悪口 (社員 / 取引先 個人攻撃)
5. 進行中 M&A / 機密案件
6. 性的内容
7. 相談 / 面談 / 個別 communication (★§1.9(k) 2026-05-27 追加 = ハラスメント・メンタル・キャリア相談 /
   1on1 / 個別面談記録 / 内部通報。社員相談ログは PII 高 risk)

【clone_memory ファイル内容】
{memory_content}

# 任務

各行を読んで、7 観点のいずれかに該当する場合は private 判定。
**判定基準**: 「これが社員 200 人に見られたら問題になるか?」
グレーは判定しない (clear private のみ)。

# 出力 (JSON only)

```json
{{
  "private_lines": [
    {{
      "line": "<該当行の元テキスト 80 字以内>",
      "category": "<個人特定/健康/家族/第三者評価/機密案件/性的/相談面談 のいずれか>",
      "reason": "<60 字以内、なぜ private か>"
    }}
  ],
  "summary": "<60 字以内、全体評価>"
}}
```

★private_lines が 0 件なら `[]` を返す (それが正常)。
★4 セクション (Profile / Ongoing Topics / Key Facts / Preferences) の **見出し行は判定対象外**。
★既存 frontmatter (---/key: value) も判定対象外。
"""


# ─── private 行除去 (★2026-06-07 cross-check で hardened、testable に分離) ──────────────
_MARK_RE = re.compile(r"^\s*(?:[-*+]\s+|\d+[.)]\s+|>\s*|#+\s*)+")
# 既知 section 見出し (= これだけ heading 扱いで除外、本文 #hashtag は除去対象に残す)
_SECTION_HEADINGS = {
    "profile", "ongoing topics", "key facts", "preferences",
    "プロフィール", "進行中の話題", "重要な事実", "好み", "嗜好", "好み・嗜好",
}


def _norm_line(s: str) -> str:
    """list/quote/heading marker を剥がして照合用に正規化 (LLM が bullet 無しで返す対策)。"""
    return _MARK_RE.sub("", s).strip()


def _is_eligible_line(ln: str) -> bool:
    """除去対象になりうる行か。section 見出し + frontmatter のみ除外、本文 #hashtag は対象。"""
    s = ln.strip()
    if not s or s.startswith("---"):
        return False
    if s.startswith("#"):
        return s.lstrip("# 　").strip().lower() not in _SECTION_HEADINGS
    return True


def _remove_private_lines(content: str, private_lines: list) -> tuple[str, list]:
    """private_lines (各 {line, category, reason}) に該当する行を content から除去。

    ★cross-check hardened: ① marker 正規化後の完全一致は重複含め全除去 (PII 完全 purge、誤爆ゼロ)
    ② 完全一致が無い時のみ部分一致 1 行 (80字 truncate 対策、20字以上)。index 指定で行ズレ無し。
    Returns: (new_content, removed_lines)。
    """
    lines = content.split("\n")
    remove_idx: set[int] = set()
    removed_lines: list = []
    for item in private_lines:
        if not isinstance(item, dict):
            continue
        text = (item.get("line") or "").strip()
        if not text:
            continue
        ntext = _norm_line(text)
        if not ntext:
            continue
        exact = [i for i, ln in enumerate(lines)
                 if i not in remove_idx and _is_eligible_line(ln) and _norm_line(ln) == ntext]
        if exact:
            for i in exact:
                remove_idx.add(i)
                removed_lines.append({"original": lines[i],
                                      "category": item.get("category"), "reason": item.get("reason")})
            continue
        if len(ntext) >= 20:
            m = next((i for i, ln in enumerate(lines)
                      if i not in remove_idx and _is_eligible_line(ln) and ntext in _norm_line(ln)), None)
            if m is not None:
                remove_idx.add(m)
                removed_lines.append({"original": lines[m],
                                      "category": item.get("category"), "reason": item.get("reason")})
    new_content = "\n".join(ln for i, ln in enumerate(lines) if i not in remove_idx)
    return new_content, removed_lines


async def review_one_user(user_id: str, dry_run: bool = False) -> dict:
    """1 user の memory を再評価し、private 行があれば archive + 除去。"""
    # 構造化ログ (★2026-05-21 bot logging 構造化)
    try:
        from bot_events import log_bot_event, bot_run_context  # type: ignore
    except Exception:
        log_bot_event = None
        bot_run_context = None

    mem_path = MEMORY_DIR / f"{user_id}.md"
    if not mem_path.exists():
        if log_bot_event:
            log_bot_event("privacy_review", "user_skipped",
                          user_id=user_id[:8], reason="no_memory", dry_run=dry_run)
        return {"user_id": user_id[:8], "skipped": True, "reason": "no_memory"}

    content = mem_path.read_text(encoding="utf-8")
    if len(content) < 200:
        if log_bot_event:
            log_bot_event("privacy_review", "user_skipped",
                          user_id=user_id[:8], reason="too_short",
                          memory_chars=len(content), dry_run=dry_run)
        return {"user_id": user_id[:8], "skipped": True, "reason": "too_short"}

    prompt = PRIVACY_REVIEW_PROMPT.format(memory_content=content[:6000])

    try:
        out = await call_llm(prompt, model="smart", max_tokens=2000, temperature=0.1)
    except Exception as e:
        logger.warning(f"[{user_id[:8]}] LLM failed: {e}")
        return {"user_id": user_id[:8], "skipped": True, "reason": f"llm_error: {e}"}

    # JSON 抽出
    # ★2026-06-07 エージェント評価: 旧 fallback regex \{[^{}]*"private_lines".*?\} は
    #   private_lines がオブジェクト配列 (= まさに private がある時) だと最初の } で切れて
    #   parse_failed → private が memory に残留する PII バグ。robust な extract_json に統一。
    from clone_improve_lib import extract_json  # type: ignore
    try:
        data = extract_json(out)
    except Exception as e:
        data = {}
    if not isinstance(data, dict) or "private_lines" not in data:
        # parse 失敗を silent skip にしない (= PII review が機能してない死角の可視化)
        logger.warning(f"[{user_id[:8]}] privacy review parse_failed → private 取りこぼし疑い")
        return {"user_id": user_id[:8], "skipped": True, "reason": "parse_failed"}

    private_lines = data.get("private_lines", [])
    summary = data.get("summary", "")

    if not private_lines:
        return {"user_id": user_id[:8], "n_private": 0, "summary": summary}

    # 該当行を memory から除去
    # ★2026-06-07 エージェント評価: 旧実装は `text in line` の部分一致 + `new_content.replace(line+"\n","")`
    #   の全文置換で、(a) text を substring に含む無関係な別行を巻込削除 (b) 同一行が複数あると全部削除
    #   の誤削除リスク。完全一致を優先し、index 指定で「その1行のみ」除去する方式に変更。
    new_content, removed_lines = _remove_private_lines(content, private_lines)

    # ★DA #4 自己監査: private を検出したのに 1 行も除去できなかった = (b)(c)(d) の取りこぼし signature。
    if private_lines and not removed_lines:
        logger.warning(f"[{user_id[:8]}] {len(private_lines)} private flagged だが 0 除去 → 取りこぼし疑い")

    if removed_lines and not dry_run:
        # archive (元行を保存)
        ARCHIVE_DIR.mkdir(parents=True, exist_ok=True)
        ts = datetime.now(JST).strftime("%Y-%m-%d")
        archive_path = ARCHIVE_DIR / f"{user_id[:12]}-{ts}.md"
        archive_path.write_text(
            f"# Archived from clone_memory ({user_id[:12]}, {ts})\n\n"
            + "\n".join(f"## {r['category']}: {r['reason']}\n> {r['original']}" for r in removed_lines),
            encoding="utf-8",
        )
        # memory 上書き
        mem_path.write_text(new_content, encoding="utf-8")
        logger.info(f"[{user_id[:8]}] archived {len(removed_lines)} lines → {archive_path.name}")

    # 構造化ログ
    if log_bot_event:
        log_bot_event("privacy_review", "user_reviewed",
                      user_id=user_id[:8],
                      n_private=len(private_lines),
                      n_removed=len(removed_lines),
                      dry_run=dry_run)

    return {
        "user_id": user_id[:8],
        "n_private": len(private_lines),
        "n_removed": len(removed_lines),
        "removed_lines": removed_lines[:5],  # 上位 5 件
        "summary": summary,
    }


def select_users_to_review(batch_size: int) -> list[str]:
    """memory ファイルから、最近 review されてない user を batch_size 件選ぶ。

    シンプル: log にある最終 review 日が古い (or 未review) 順。
    """
    if not MEMORY_DIR.exists():
        return []
    # 全 user の (user_id, mem mtime) リスト
    candidates = []
    for f in MEMORY_DIR.glob("*.md"):
        try:
            candidates.append((f.stem, f.stat().st_mtime))
        except Exception:
            continue

    # 過去の review log を読んで「最後に review した日」を user 別に取得
    last_reviewed: dict[str, float] = {}
    if LOG_PATH.exists():
        for line in LOG_PATH.read_text(encoding="utf-8").splitlines():
            try:
                r = json.loads(line)
                ts = r.get("timestamp", "")
                rt = datetime.fromisoformat(ts.replace("Z", "+00:00")).timestamp()
                for u in r.get("reviewed_users", []):
                    last_reviewed[u] = max(last_reviewed.get(u, 0), rt)
            except Exception:
                continue

    # last_reviewed が古い順 (or 未 review) で並べる
    candidates.sort(key=lambda x: last_reviewed.get(x[0], 0))
    return [c[0] for c in candidates[:batch_size]]


async def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--user", help="1 user だけ review")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--batch-size", type=int, default=DAILY_BATCH_SIZE)
    args = ap.parse_args()

    ensure_dirs()
    REVIEW_DIR.mkdir(parents=True, exist_ok=True)

    today = datetime.now(JST).strftime("%Y-%m-%d")

    if args.user:
        users = [args.user]
    else:
        users = select_users_to_review(args.batch_size)

    if not users:
        logger.info("review すべき user 無し、skip")
        return 0

    logger.info(f"=== privacy_review {today} ({len(users)} users) ===")
    results = []
    total_removed = 0
    for uid in users:
        r = await review_one_user(uid, dry_run=args.dry_run)
        results.append(r)
        total_removed += r.get("n_removed", 0)

    summary = {
        "date": today,
        "timestamp": datetime.now(JST).isoformat(),
        "reviewed_users": users,
        "total_lines_removed": total_removed,
        "user_results": results,
    }

    # 出力
    out_path = REVIEW_DIR / f"{today}.json"
    out_path.write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
    append_jsonl(LOG_PATH, {"timestamp": summary["timestamp"], "date": today,
                            "reviewed_users": users, "total_removed": total_removed})

    logger.info(f"done: {len(users)} reviewed, {total_removed} lines removed")

    # ★2026-06-07 DA cross-check #2/#4: 大量除去だけでなく parse 失敗 / 取りこぼし でも LINE Push。
    #   従来は total_removed>=5 のみ通知 → 全 user parse_failed (= PII 残留) でも total_removed=0 で
    #   無音 = 「privacy gate が静かに不発」の典型 silent-fail。fail-loud にする。
    n_parse_failed = sum(1 for r in results if r.get("reason") == "parse_failed")
    n_retained = sum(1 for r in results
                     if r.get("n_private", 0) > 0 and r.get("n_removed", 0) == 0)
    if not args.dry_run and (total_removed >= 5 or n_parse_failed or n_retained):
        warn = []
        if n_parse_failed:
            warn.append(f"⚠️ JSON parse 失敗 {n_parse_failed} 名 (= PII 取りこぼし疑い、review 不発)")
        if n_retained:
            warn.append(f"⚠️ private 検出も 0 除去 {n_retained} 名 (= 行照合の取りこぼし疑い)")
        line_push(
            f"🔒 clone_memory privacy review ({today})\n"
            f"reviewed: {len(users)} 名 / 除去行: {total_removed} 件\n"
            + ("\n".join(warn) + "\n" if warn else "")
            + f"詳細: {out_path}"
        )

    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
