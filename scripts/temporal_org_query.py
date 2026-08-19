#!/usr/bin/env python3
"""
temporal_org_query.py — 組織情報の時系列クエリ helper (項目 5 Phase 1)

graph DB を入れない軽量実装。store master の月次 snapshot を bi-temporal で保持し、
「半年前の中田AMの担当 SV」「FY25 当時の○○店の店長」型の query に答える。

設計:
  - snapshot 場所: data/brain/wiki/knowledge/history/org-snapshots/YYYY-MM-DD.json
  - 各 snapshot に valid_from / valid_to で適用期間を記録
  - クエリで「N ヶ月前 / FY25 / YYYY年M月」等の時点指定が来たら、
    該当 snapshot から特定の店舗 / AM / SV の状態を抽出

CLI:
  python3 scripts/temporal_org_query.py --date 2026-05-15
  python3 scripts/temporal_org_query.py --date 2026-04-01 --am 中田将也
  python3 scripts/temporal_org_query.py --date 2026-05-15 --store 池袋西口
  python3 scripts/temporal_org_query.py --date 2026-05-15 --sv 鈴木和典
  python3 scripts/temporal_org_query.py --history-am 中田将也

brain_wiki.py から import で呼ばれる:
  from scripts.temporal_org_query import (
      snapshot_at, query_am_at, query_sv_at, query_store_at, history_for_am,
  )

将来 Phase 2:
  Graphiti / Neo4j 統合で multi-hop ("中田AMが管轄してた店のうち今は他 AM" 等)
"""
from __future__ import annotations

import argparse
import json
from datetime import date, datetime
from pathlib import Path
from typing import Optional

# ─── path ────────────────────────────────────────────────────────
def _snapshot_dir() -> Path:
    """snapshot ディレクトリを返す。docker /app と local repo の両対応。"""
    # docker container 内 (/app/data/brain/wiki/knowledge/history/org-snapshots)
    candidates = [
        Path("/app/data/brain/wiki/knowledge/history/org-snapshots"),
        Path(__file__).resolve().parent.parent / "data" / "brain" / "wiki" / "knowledge" / "history" / "org-snapshots",
    ]
    for c in candidates:
        if c.exists():
            return c
    return candidates[-1]  # 無くても repo root の path を返す


SNAPSHOT_DIR = _snapshot_dir()


def _parse_date(s: str) -> date:
    """YYYY-MM-DD or YYYY-MM (月初扱い) を date に変換。"""
    s = s.strip()
    if len(s) == 7:  # YYYY-MM
        s += "-01"
    return datetime.strptime(s, "%Y-%m-%d").date()


# ─── snapshot 検索 ────────────────────────────────────────────────
def list_snapshots() -> list[dict]:
    """全 snapshot を読み込み、新しい順で返す。"""
    if not SNAPSHOT_DIR.exists():
        return []
    out = []
    for f in sorted(SNAPSHOT_DIR.glob("*.json")):
        try:
            d = json.loads(f.read_text(encoding="utf-8"))
            d["_path"] = str(f)
            out.append(d)
        except Exception:
            continue
    return out


def snapshot_at(target_date: date | str) -> Optional[dict]:
    """指定日に有効な snapshot を返す。

    各 snapshot の valid_from <= target_date <= valid_to なら採用。
    valid_to が空なら無期限有効として扱う。
    どれにも当たらない場合は **最新 snapshot** を fallback で返す。
    """
    if isinstance(target_date, str):
        target_date = _parse_date(target_date)

    snaps = list_snapshots()
    if not snaps:
        return None
    for s in snaps:
        vf = s.get("valid_from")
        vt = s.get("valid_to")
        if not vf:
            continue
        try:
            vf_d = _parse_date(vf)
        except Exception:
            continue
        try:
            vt_d = _parse_date(vt) if vt else date(2099, 12, 31)
        except Exception:
            vt_d = date(2099, 12, 31)
        if vf_d <= target_date <= vt_d:
            return s
    # fallback: 最新 snapshot
    return snaps[-1]


# ─── 個別 query ───────────────────────────────────────────────────
def query_am_at(target_date: date | str, am_name: str) -> dict:
    """指定日における AM の担当: SV list + 店舗 list + エリア。"""
    snap = snapshot_at(target_date)
    if not snap:
        return {"error": "no_snapshot", "target_date": str(target_date)}
    stores = [s for s in snap["stores"] if s.get("am") == am_name]
    svs = sorted(set(s.get("sv") for s in stores if s.get("sv")))
    areas = sorted(set(s.get("area") for s in stores if s.get("area")))
    return {
        "as_of": snap.get("snapshot_date"),
        "valid_from": snap.get("valid_from"),
        "valid_to": snap.get("valid_to"),
        "phase": snap.get("phase"),
        "am_name": am_name,
        "store_count": len(stores),
        "sv_list": svs,
        "areas": areas,
        "stores": [
            {"code": s.get("code"), "name": s.get("name"), "sv": s.get("sv"),
             "area": s.get("area"), "prefecture": s.get("prefecture")}
            for s in stores
        ],
    }


def query_sv_at(target_date: date | str, sv_name: str) -> dict:
    """指定日における SV の担当: AM + 店舗 list。"""
    snap = snapshot_at(target_date)
    if not snap:
        return {"error": "no_snapshot", "target_date": str(target_date)}
    stores = [s for s in snap["stores"] if s.get("sv") == sv_name]
    ams = sorted(set(s.get("am") for s in stores if s.get("am")))
    areas = sorted(set(s.get("area") for s in stores if s.get("area")))
    return {
        "as_of": snap.get("snapshot_date"),
        "valid_from": snap.get("valid_from"),
        "phase": snap.get("phase"),
        "sv_name": sv_name,
        "store_count": len(stores),
        "am_above": ams,
        "areas": areas,
        "stores": [
            {"code": s.get("code"), "name": s.get("name"),
             "area": s.get("area"), "prefecture": s.get("prefecture")}
            for s in stores
        ],
    }


def query_store_at(target_date: date | str, store_keyword: str) -> dict:
    """指定日における店舗の状態 (店長/AM/SV/area)。"""
    snap = snapshot_at(target_date)
    if not snap:
        return {"error": "no_snapshot", "target_date": str(target_date)}
    matches = [s for s in snap["stores"]
               if store_keyword in (s.get("name") or "")
               or str(s.get("code")) == store_keyword]
    return {
        "as_of": snap.get("snapshot_date"),
        "valid_from": snap.get("valid_from"),
        "phase": snap.get("phase"),
        "store_keyword": store_keyword,
        "matches_count": len(matches),
        "matches": matches[:20],
    }


def history_for_am(am_name: str) -> list[dict]:
    """AM 配置の時系列履歴 (全 snapshot を時系列で並べる)。"""
    snaps = list_snapshots()
    history = []
    for s in snaps:
        stores = [st for st in s["stores"] if st.get("am") == am_name]
        if not stores:
            continue
        history.append({
            "valid_from": s.get("valid_from"),
            "valid_to": s.get("valid_to"),
            "phase": s.get("phase"),
            "store_count": len(stores),
            "sv_list": sorted(set(st.get("sv") for st in stores if st.get("sv"))),
            "areas": sorted(set(st.get("area") for st in stores if st.get("area"))),
        })
    return history


def history_for_store(store_keyword: str) -> list[dict]:
    """店舗の配置時系列 (店長/AM/SV/area の歴史)。"""
    snaps = list_snapshots()
    history = []
    for s in snaps:
        matches = [st for st in s["stores"]
                   if store_keyword in (st.get("name") or "")
                   or str(st.get("code")) == store_keyword]
        for m in matches:
            history.append({
                "valid_from": s.get("valid_from"),
                "valid_to": s.get("valid_to"),
                "phase": s.get("phase"),
                "code": m.get("code"),
                "name": m.get("name"),
                "am": m.get("am"),
                "sv": m.get("sv"),
                "area": m.get("area"),
                "manager": m.get("manager"),
            })
    return history


# ─── relative date 解釈 (brain_wiki 統合用) ──────────────────────
def resolve_relative_date(query: str, today: Optional[date] = None) -> Optional[date]:
    """query から相対日付を解釈 (簡易版)。

    対応: 「N ヶ月前 / N 年前 / 半年前 / 去年 / 昨年 / 一年前 / 一昨年 / FY25 / 2026 年 4 月」
    """
    import re
    today = today or date.today()
    q = query
    if "半年前" in q or "6 ヶ月前" in q or "6ヶ月前" in q:
        return date(today.year, today.month, 1).replace(
            month=((today.month - 6 - 1) % 12) + 1,
            year=today.year - (1 if today.month <= 6 else 0),
        )
    m = re.search(r"(\d+)\s*ヶ月前", q)
    if m:
        n = int(m.group(1))
        month = ((today.month - n - 1) % 12) + 1
        year = today.year - ((today.month - n - 1) // 12 * -1)  # 簡易、12 ヶ月前以上は粗い
        try:
            return date(year, month, 1)
        except Exception:
            return None
    if "去年" in q or "昨年" in q or "1 年前" in q or "一年前" in q:
        return date(today.year - 1, today.month, 1)
    if "一昨年" in q or "2 年前" in q or "二年前" in q:
        return date(today.year - 2, today.month, 1)
    m = re.search(r"FY(\d{2})", q)
    if m:
        fy = 2000 + int(m.group(1))
        # FY27 = 2026-04 to 2027-03 (OWNDAYS は 4 月始まり)
        return date(fy - 1, 4, 1)
    m = re.search(r"(\d{4})\s*年\s*(\d{1,2})\s*月", q)
    if m:
        try:
            return date(int(m.group(1)), int(m.group(2)), 1)
        except Exception:
            pass
    return None


# ─── CLI ─────────────────────────────────────────────────────────
def _cli():
    ap = argparse.ArgumentParser()
    ap.add_argument("--date", help="YYYY-MM-DD or YYYY-MM (default: today)")
    ap.add_argument("--am", help="AM 名で query")
    ap.add_argument("--sv", help="SV 名で query")
    ap.add_argument("--store", help="店舗名 or 店番で query")
    ap.add_argument("--history-am", help="AM の時系列履歴")
    ap.add_argument("--history-store", help="店舗の時系列履歴")
    ap.add_argument("--list", action="store_true", help="snapshot 一覧")
    args = ap.parse_args()

    if args.list:
        snaps = list_snapshots()
        for s in snaps:
            print(f"- {s.get('snapshot_date')} (valid {s.get('valid_from')} → {s.get('valid_to') or '∞'}) "
                  f"phase={s.get('phase')} stores={len(s.get('stores', []))}")
        return

    if args.history_am:
        h = history_for_am(args.history_am)
        print(json.dumps(h, ensure_ascii=False, indent=2))
        return

    if args.history_store:
        h = history_for_store(args.history_store)
        print(json.dumps(h, ensure_ascii=False, indent=2))
        return

    target = args.date or datetime.now().strftime("%Y-%m-%d")
    if args.am:
        r = query_am_at(target, args.am)
    elif args.sv:
        r = query_sv_at(target, args.sv)
    elif args.store:
        r = query_store_at(target, args.store)
    else:
        s = snapshot_at(target)
        if s:
            r = {
                "snapshot": s.get("snapshot_date"),
                "valid_from": s.get("valid_from"),
                "valid_to": s.get("valid_to"),
                "phase": s.get("phase"),
                "stores_count": len(s.get("stores", [])),
            }
        else:
            r = {"error": "no_snapshot"}
    print(json.dumps(r, ensure_ascii=False, indent=2, default=str))


if __name__ == "__main__":
    _cli()
