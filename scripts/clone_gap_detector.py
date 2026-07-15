#!/usr/bin/env python3
"""scripts/clone_gap_detector.py — クローンが「データ不足」で答えられなかった質問を検知(★2026-07-01 海山指示)。

うみやまAI が「データに入ってない/確認できてない」等で deflection した data/財務 質問を clone_history から拾い、
**未処理の新規分**を海山へ push。海山が「今後拡充する予定」で溜めていた穴を少しずつ埋めるための backlog。

分類(rule-based、粗く):
  A-fill      = OWNDAYS が持たない/wiki 未整備 → 海山がデータ提供 → wiki/canon へ ingest
  B-retrieval = 既存 scraper/master にあるはず(店舗別売上・閉店・坪数等)→ retrieval/集計の改善

実行: python3 scripts/clone_gap_detector.py [--push] [--all]  (host cron 週次、--push で新規のみ LINE)
"""
from __future__ import annotations

import argparse
import glob
import json
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from clone_improve_lib import line_push  # noqa: E402

ROOT = Path(__file__).resolve().parents[1]
HISTORY_DIR = ROOT / "data" / "brain" / "clone_history"
STATE_FILE = ROOT / "data" / "brain" / ".clone_gap_state.json"

DEFLECT = re.compile(r"データに入って|確認できてない|把握でき|把握してない|情報がな|わからな|今後.{0,6}拡充|拡充.{0,6}予定|まだ.{0,4}整備|準備中")
DATA = re.compile(r"EBITDA|売上|利益|粗利|成長|店舗|予算|単価|客数|在庫|KPI|前年|目標|FY2|Q[1-4]|インバウンド|閉店|出店|坪|億|万本|販売本数")
# 「データを尋ねている」質問だけを対象(詩的独白・相槌・文脈断片を除外)
QUESTION = re.compile(r"[?？]\s*$|教えて|いくら|どこ|何店|何本|ですか|平均|対比|坪数|坪面積|達成でき")
# B = 既存 scraper/master で答えられるはず(店舗別売上・閉店・坪・prefecture 集計・前年対比)
B_HINT = re.compile(r"店舗.*売[上り]|売[上り].*店舗|閉店|坪数|坪面積|前年対比|前年比|各店|店舗数|月次|月間平均|都道府県|県.*売[上り]|インバウンド")
# A = そもそも wiki/scraper に無い(過去年の沿革・買収・特定ブランド・予算達成見込み等)
A_HINT = re.compile(r"200[0-9]|201[0-9]|買収|創業|沿革|Meller|メレル|予算.*達成|達成.*見込")


def _classify(q: str) -> str:
    if A_HINT.search(q):
        return "A-fill"
    return "B-retrieval" if B_HINT.search(q) else "A-fill"


def scan_gaps() -> list[dict]:
    gaps = []
    for f in glob.glob(str(HISTORY_DIR / "*.jsonl")):
        last_user, last_ts = None, None
        for line in open(f, encoding="utf-8", errors="ignore"):
            try:
                e = json.loads(line)
            except Exception:
                continue
            role, text = e.get("role"), (e.get("text") or "")
            if role == "user":
                last_user, last_ts = text, (e.get("timestamp") or "")[:10]
            elif role == "assistant" and DEFLECT.search(text):
                q = (last_user or "").strip().replace("\n", " ")
                if 3 < len(q) < 160 and DATA.search(q) and QUESTION.search(q):
                    gaps.append({"date": last_ts, "q": q[:140], "cat": _classify(q),
                                 "key": re.sub(r"\s+", "", q)[:50]})
    # dedup by key (同じ質問は1つ)
    seen, uniq = set(), []
    for g in sorted(gaps, key=lambda x: x["date"] or "", reverse=True):
        if g["key"] in seen:
            continue
        seen.add(g["key"])
        uniq.append(g)
    return uniq


def _load_done() -> set:
    if STATE_FILE.exists():
        try:
            return set(json.loads(STATE_FILE.read_text(encoding="utf-8")).get("done", []))
        except Exception:
            pass
    return set()


def run(push: bool = False, show_all: bool = False) -> dict:
    gaps = scan_gaps()
    done = _load_done()
    fresh = gaps if show_all else [g for g in gaps if g["key"] not in done]
    if not fresh:
        return {"ok": True, "new": 0, "total": len(gaps)}
    a = [g for g in fresh if g["cat"] == "A-fill"]
    b = [g for g in fresh if g["cat"] == "B-retrieval"]
    lines = [f"🧩 うみやまAI が答えられなかったデータ質問 {len(fresh)}件(埋める候補)", ""]
    if b:
        lines.append("■ B(既存データで答えられるはず=retrieval改善):")
        lines += [f"・{g['q']}（{g['date']}）" for g in b[:8]]
    if a:
        lines.append("■ A(データ提供が要る=wiki/canon拡充):")
        lines += [f"・{g['q']}（{g['date']}）" for g in a[:8]]
    lines.append("\n→ Aは元データを教えてもらえれば取込、Bは私が retrieval を直します。")
    msg = "\n".join(lines)
    if push:
        try:
            line_push(msg)
        except Exception:
            pass
        STATE_FILE.write_text(json.dumps({"done": [g["key"] for g in gaps]}, ensure_ascii=False),
                              encoding="utf-8")
    else:
        print(msg)
    return {"ok": True, "new": len(fresh), "A": len(a), "B": len(b), "total": len(gaps)}


def main() -> int:
    ap = argparse.ArgumentParser(description="クローンのデータ回答ギャップ検知")
    ap.add_argument("--push", action="store_true", help="新規ギャップを LINE push + state 更新")
    ap.add_argument("--all", action="store_true", help="既処理含め全件表示(state 無視)")
    a = ap.parse_args()
    print(run(push=a.push, show_all=a.all))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
