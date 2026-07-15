"""
clone_auto_improve.py — うみやまAI 日次 自動改善エージェント

直近 24h の会話ログを LLM (Claude smart) に投げて 7 種の signal を検知:
- knowledge_gap, rephrase_retry, correction, style_mismatch,
  uncovered_cluster, judgment_drift, decision_conflict

検知結果に応じて:
- auto_applied: 実際に wiki 編集 (max 10/日、超過は drafts/queue/)
- drafts_for_review: 海山判断待ち (drafts/judgment/ or drafts/decisions/)

ログ: data/brain/clone_improve/auto_edit_log.jsonl

cron: 毎日 03:00 JST
  python3 scripts/clone_auto_improve.py
"""
from __future__ import annotations

import asyncio
import json
import logging
import sys
from datetime import datetime, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from clone_improve_lib import (
    ensure_dirs, load_conversations, call_llm, extract_json,
    append_jsonl, safe_write_wiki, wiki_path, line_push, loud_fail, supervisor_model,
    IMPROVE_DIR, DRAFTS_DIR, QUEUE_DIR, AUTO_EDIT_LOG, WIKI_DIR, JST,
)

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger("clone_auto_improve")

MAX_AUTO_EDITS_PER_DAY = 10


def load_wiki_index() -> str:
    """wiki/index.md (compile 出力) があれば。無ければ wiki/ の主要ファイル list で代替。"""
    idx = WIKI_DIR / "index.md"
    if idx.exists():
        return idx.read_text(encoding="utf-8")[:5000]
    # fallback: knowledge/ 配下の wiki list
    lines = ["# Wiki Index (fallback)"]
    for sub in ["knowledge", "people", "projects", "decisions", "style"]:
        d = WIKI_DIR / sub
        if not d.exists():
            continue
        lines.append(f"\n## {sub}/")
        for f in sorted(d.glob("*.md"))[:30]:
            lines.append(f"- {sub}/{f.name}")
    return "\n".join(lines)


def format_conversations(records: list[dict], max_chars: int = 30000) -> str:
    """会話ログを LLM に渡す形式に整形。個人情報配慮で user_id は短縮 hash。"""
    out = []
    total = 0
    for r in records:
        uid = r.get("user_id", "?")[:8]
        role = r.get("role", "?")
        ts = r.get("timestamp", "")[:16]
        text = r.get("text", "").replace("\n", " ")[:300]
        line = f"[{ts}] {uid} ({role}): {text}"
        if total + len(line) > max_chars:
            out.append(f"...(以下 {len(records) - len(out)} 件省略)")
            break
        out.append(line)
        total += len(line)
    return "\n".join(out)


PROMPT_TEMPLATE = """あなたは「うみやまAI」の自動改善エージェントです。
うみやまAIは OWNDAYS CEO 海山丈司のクローンとして社員の質問に答えるシステム。
目的は「より使われるAIになる」こと。利用頻度・満足度・再利用率を上げるため、
Brain Wikiを毎日自動で改善します。

# 入力
<conversation_logs>
{conversation_logs}
</conversation_logs>

<wiki_index>
{wiki_index}
</wiki_index>

# 検知シグナル → 自動アクション対応表

| シグナル | 検知条件 | アクション |
|---|---|---|
| knowledge_gap | AIが「該当情報なし」/抽象論しか返せず | `knowledge/*.md` 新規作成 or 加筆 (自動実行) |
| rephrase_retry | 同社員が7日内に同義質問を言い換え再投 | 該当wiki記事に「よくある聞き方」セクション追記 (自動実行) |
| correction | 社員が「違う/そうじゃない」と訂正 | 該当wikiの該当箇所を訂正版に書き換え (自動実行) |
| style_mismatch | AIが冗長・回りくどい・一般論的 | `style.md` に回答パターン例を追記 (自動実行) |
| uncovered_cluster | 3件以上問われたがwiki記事なし | `knowledge/<topic>.md` 新規作成 (自動実行) |
| judgment_drift | 海山の既存判断軸と食い違う回答 | **書き換えはしない**。ドラフトを `drafts/judgment/` に出力 |
| decision_conflict | decisions/ と食い違う回答 | **書き換えはしない**。ドラフトを `drafts/decisions/` に出力 |

# 厳守ルール

1. 既存wiki更新時は元ファイル末尾に追記、または該当セクションのみ差し替え。全文上書き禁止。
2. 加筆内容は海山スタイル (直接的・簡潔・80%主義、出典伏せ、自慢しない、コーティングは控えめ) で書く。
3. 既存ファイルとの矛盾を発見したら自動更新を止めて drafts/ に回す。
4. 1日あたり自動編集は最大 {max_edits} ファイル。それ以上は drafts/queue/ に積む。
5. 各編集に `auto_edit_log` を残す: 元ファイルパス, 編集差分, 根拠ログ引用, 日時。
6. **個人特定情報 (社員実名/個別評価/家族プライベート/M&A 進行中等) は wiki に書かない**。集計値・パターン化された内容のみ。

# 出力 (JSON only、コードブロックで)

```json
{{
  "date": "{date}",
  "auto_applied": [
    {{
      "file": "knowledge/<filename>.md",
      "operation": "create" | "append" | "replace_section",
      "section_anchor": "## XXX" (replace_section の場合のみ),
      "content": "...海山スタイルで書かれた本文 (markdown)...",
      "evidence_quotes": ["社員質問原文40字以内", "AI回答原文40字以内"],
      "expected_impact": "棚卸関連質問のknowledge_gap解消"
    }}
  ],
  "drafts_for_review": [
    {{
      "file": "drafts/judgment/{date}-<slug>.md",
      "reason": "AIが慎重路線を勧めたが speed-over-precision-002 と食い違い",
      "proposed_diff": "...",
      "evidence_quotes": ["..."]
    }}
  ],
  "metrics_snapshot": {{
    "total_conversations": 0,
    "knowledge_gap_count": 0,
    "rephrase_retry_count": 0,
    "abandon_count": 0,
    "auto_edits_applied": 0,
    "drafts_queued": 0
  }}
}}
```

★ JSON 以外の余計なテキストを出力しない。
"""


def _drain_overflow_queue(now, budget: int) -> int:
    """前日までに queue/ へ溢れた auto-edit を budget 件まで適用して回収(腐敗防止、★2026-06-20)。

    一度処理した queue file は成功/陳腐どちらでも除去し、無限滞留させない(stale anchor は safe_write_wiki が no-op)。
    """
    if budget <= 0 or not QUEUE_DIR.exists():
        return 0
    applied = 0
    for qf in sorted(QUEUE_DIR.glob("*.json")):
        if applied >= budget:
            break
        try:
            item = json.loads(qf.read_text(encoding="utf-8"))
        except Exception:
            qf.unlink(missing_ok=True)
            continue
        file_rel = item.get("file", "")
        content = item.get("content", "")
        op = item.get("operation", "append")
        if not file_rel or not content or file_rel.startswith("drafts/"):
            qf.unlink(missing_ok=True)
            continue
        mode = {"create": "create", "append": "append",
                "replace_section": "replace_section"}.get(op, "append")
        anchor = item.get("section_anchor", "") if op == "replace_section" else ""
        try:
            ok = safe_write_wiki(file_rel, content, mode=mode, section_anchor=anchor)
        except Exception as e:
            logger.warning(f"drain apply error {file_rel}: {e}")
            ok = False
        if ok:
            applied += 1
            append_jsonl(AUTO_EDIT_LOG, {"timestamp": now.isoformat(), "file": file_rel,
                                         "operation": op, "drained": True,
                                         "content_preview": content[:300]})
            logger.info(f"drained+applied: {file_rel} ({op})")
        else:
            logger.info(f"drain discard (stale/no-op): {file_rel}")
        qf.unlink(missing_ok=True)
    return applied


async def main():
    ensure_dirs()
    now = datetime.now(JST)
    since = now - timedelta(hours=24)
    target_date = now.strftime("%Y-%m-%d")

    logger.info(f"=== clone_auto_improve {target_date} ===")
    records = load_conversations(since)
    logger.info(f"records (last 24h): {len(records)}")
    if not records:
        logger.info("no records, skip")
        return

    wiki_idx = load_wiki_index()
    conv_text = format_conversations(records)

    prompt = PROMPT_TEMPLATE.format(
        conversation_logs=conv_text,
        wiki_index=wiki_idx,
        max_edits=MAX_AUTO_EDITS_PER_DAY,
        date=target_date,
    )

    try:
        # ★2026-07-10 監督者層 = Fable 5 (litellm supervisor、fallback: smart→smart-fallback)
        out = await call_llm(prompt, model=supervisor_model(), max_tokens=16000, temperature=None)
        data = extract_json(out)
        # §1.18: 日次自己改善の成否確定点 (旧: except → exit 0 の silent 死 = DA 指摘)
        loud_fail("clone_auto_improve_llm", True)
    except Exception as e:
        logger.error(f"LLM call/parse failed: {e}")
        loud_fail("clone_auto_improve_llm", False, f"LLM call/parse failed: {e}")
        return

    # ★2026-06-20 overflow 回収: 前日までに溢れた edit を先に適用し、残 budget で新規適用(合計 ≤ 日次上限)
    budget = MAX_AUTO_EDITS_PER_DAY - _drain_overflow_queue(now, MAX_AUTO_EDITS_PER_DAY)
    auto_applied = data.get("auto_applied", [])[:budget]
    drafts = data.get("drafts_for_review", [])

    # === 自動適用 ===
    applied_count = 0
    for item in auto_applied:
        file_rel = item.get("file", "")
        op = item.get("operation", "")
        content = item.get("content", "")
        if not file_rel or not content:
            continue
        if file_rel.startswith("drafts/"):
            # drafts は applied 側に来ない設計、念のため弾く
            continue
        try:
            # ★2026-06-07 エージェント評価: 旧 replace_section→overwrite map は section content で
            #   wiki 全文を消失させていた。真の section 置換 (anchor 指定) に。anchor 未発見/無しなら
            #   safe_write_wiki が False を返し no-op (= 全文上書きしない安全側)。
            mode = {"create": "create", "append": "append",
                    "replace_section": "replace_section"}.get(op, "append")
            anchor = item.get("section_anchor", "") if op == "replace_section" else ""
            ok = safe_write_wiki(file_rel, content, mode=mode, section_anchor=anchor)
            if ok:
                applied_count += 1
                append_jsonl(AUTO_EDIT_LOG, {
                    "timestamp": now.isoformat(),
                    "file": file_rel,
                    "operation": op,
                    "evidence_quotes": item.get("evidence_quotes", []),
                    "expected_impact": item.get("expected_impact", ""),
                    "content_preview": content[:300],
                })
                logger.info(f"applied: {file_rel} ({op})")
            else:
                logger.warning(f"failed to apply: {file_rel}")
        except Exception as e:
            logger.warning(f"apply error {file_rel}: {e}")

    # 超過分は queue/ へ(翌日 _drain_overflow_queue が回収)
    overflow = data.get("auto_applied", [])[budget:]
    for item in overflow:
        slug = (item.get("file", "queued") or "queued").replace("/", "_")
        qpath = QUEUE_DIR / f"{target_date}-{slug}.json"
        qpath.write_text(json.dumps(item, ensure_ascii=False, indent=2), encoding="utf-8")

    # === drafts (要レビュー) ===
    draft_count = 0
    urgent_drafts = []
    for d in drafts:
        path_rel = d.get("file", "")
        if not path_rel.startswith("drafts/"):
            continue
        full = IMPROVE_DIR / path_rel
        full.parent.mkdir(parents=True, exist_ok=True)
        body = (
            f"# {d.get('reason', '(no reason)')}\n\n"
            f"## 提案 diff\n\n{d.get('proposed_diff', '')}\n\n"
            f"## 根拠\n\n"
            + "\n".join(f"- {q}" for q in d.get("evidence_quotes", []))
        )
        full.write_text(body, encoding="utf-8")
        draft_count += 1
        if "judgment" in path_rel or "decision" in path_rel:
            urgent_drafts.append(d)

    # === 日次ログ ===
    summary = {
        "date": target_date,
        "auto_edits_applied": applied_count,
        "drafts_queued": draft_count,
        "queue_overflow": len(overflow),
        "metrics_snapshot": data.get("metrics_snapshot", {}),
    }
    append_jsonl(IMPROVE_DIR / "daily_summary.jsonl", summary)

    # === 緊急 drafts は即 LINE Push (judgment_drift / decision_conflict) ===
    if urgent_drafts:
        msg = f"🔧 うみやまAI 自動改善 ({target_date}):\n"
        msg += f"自動適用 {applied_count} 件 / レビュー待ち {draft_count} 件\n"
        msg += f"\n緊急 (判断軸/decision conflict):\n"
        for d in urgent_drafts[:3]:
            msg += f"- {d.get('reason', '')[:80]}\n"
        msg += f"\n詳細: /Users/brain/brain-agent/data/brain/clone_improve/drafts/"
        line_push(msg)

    logger.info(f"done. applied={applied_count} drafts={draft_count}")
    print(json.dumps(summary, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    asyncio.run(main())
