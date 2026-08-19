"""
knowledge_graph.py — 軽量 in-process knowledge graph (Phase 1)

設計:
  本格的には Neo4j + Graphiti を docker compose に立てるべきだが、
  Phase 1 は外部 DB 無しで multi-hop query を動かす最小実装。

  - 既存 org-snapshots/<date>.json (304 店 / 6 AM / 27 SV / 38 都道府県) を input
  - in-memory dict-based graph で multi-hop traversal
  - bi-temporal (snapshot 単位で時間軸を持つ、過去日付指定可)

Phase 1 で答えられるクエリ:
  - 「中田AM が現在管轄してる店一覧」 (1-hop: am → stores)
  - 「中田AM が管轄してた店のうち今は別 AM」 (2-hop temporal diff)
  - 「鈴木 SV の AM (上司) は誰?」 (1-hop: sv → am)
  - 「ある SV が直近 6 ヶ月で AM 替わったか」 (temporal trace)
  - 「同じ都道府県に複数 AM がいるか」 (1-hop: prefecture → am)

Phase 2 (将来):
  - Neo4j + Graphiti 統合 (multi-tenant、永続化、Cypher クエリ)
  - 「中田AM の管轄店出身の店長がいる店」型の 3-hop+
  - LLM 経由の natural language query → Cypher 自動生成

実行:
  python3 scripts/knowledge_graph.py --list-snapshots
  python3 scripts/knowledge_graph.py --build 2026-05-19           # load snapshot
  python3 scripts/knowledge_graph.py --query am-stores "中田将也"
  python3 scripts/knowledge_graph.py --query diff-am-stores "中田将也" \\
                                            --from 2026-04-01 --to 2026-05-19
"""
from __future__ import annotations

import argparse
import json
import logging
import os
import sys
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger("knowledge_graph")

APP_ROOT = Path(os.getenv("BRAIN_APP_ROOT", "/app"))
WIKI_DIR = APP_ROOT / "data" / "brain" / "wiki"
SNAPSHOT_DIR = WIKI_DIR / "knowledge" / "history" / "org-snapshots"


# ─── ノード型 ────────────────────────────────
@dataclass
class Store:
    code: int | None
    name: str
    prefecture: str
    area: str
    type: str  # 直営 / FC / EC
    league: str
    am: str
    sv: str
    manager: str


@dataclass
class Person:
    """AM / SV / 店長 などのヒト。ロールと所属店舗で曖昧性を持つ。"""
    name: str
    role: str  # AM | SV | 店長


@dataclass
class OrgGraph:
    """1 snapshot 分の組織グラフ。bi-temporal 用に snapshot 日付を持つ。"""
    snapshot_date: str  # YYYY-MM-DD
    valid_from: str | None = None
    valid_to: str | None = None
    stores: dict[int, Store] = field(default_factory=dict)
    # 索引 (lookup 高速化用):
    by_am: dict[str, list[int]] = field(default_factory=dict)        # am_name → store codes
    by_sv: dict[str, list[int]] = field(default_factory=dict)        # sv_name → store codes
    by_prefecture: dict[str, list[int]] = field(default_factory=dict)
    by_manager: dict[str, list[int]] = field(default_factory=dict)
    am_to_svs: dict[str, set[str]] = field(default_factory=dict)     # am_name → sv_names
    sv_to_am: dict[str, str] = field(default_factory=dict)           # sv_name → am_name


def load_snapshot(snapshot_date: str) -> OrgGraph:
    """org-snapshots/<date>.json を読み込んで OrgGraph に。"""
    p = SNAPSHOT_DIR / f"{snapshot_date}.json"
    if not p.exists():
        raise SystemExit(f"snapshot not found: {p}")
    raw = json.loads(p.read_text(encoding="utf-8"))

    g = OrgGraph(
        snapshot_date=raw.get("snapshot_date", snapshot_date),
        valid_from=raw.get("valid_from"),
        valid_to=raw.get("valid_to"),
    )
    for s_dict in raw.get("stores", []):
        code = s_dict.get("code")
        if code is None:
            continue
        store = Store(
            code=int(code),
            name=s_dict.get("name", ""),
            prefecture=s_dict.get("prefecture", ""),
            area=s_dict.get("area", ""),
            type=s_dict.get("type", "直営"),
            league=s_dict.get("league", ""),
            am=s_dict.get("am", "") or "",
            sv=s_dict.get("sv", "") or "",
            manager=s_dict.get("manager", "") or "",
        )
        g.stores[store.code] = store
        if store.am:
            g.by_am.setdefault(store.am, []).append(store.code)
        if store.sv:
            g.by_sv.setdefault(store.sv, []).append(store.code)
            if store.am:
                g.sv_to_am[store.sv] = store.am
                g.am_to_svs.setdefault(store.am, set()).add(store.sv)
        if store.prefecture:
            g.by_prefecture.setdefault(store.prefecture, []).append(store.code)
        if store.manager:
            g.by_manager.setdefault(store.manager, []).append(store.code)
    return g


def list_snapshots() -> list[str]:
    if not SNAPSHOT_DIR.exists():
        return []
    return sorted([f.stem for f in SNAPSHOT_DIR.glob("*.json")])


# ─── multi-hop query ─────────────────────────
def query_am_stores(g: OrgGraph, am_name: str) -> list[Store]:
    """AM の管轄店一覧。"""
    codes = g.by_am.get(am_name, [])
    return [g.stores[c] for c in codes if c in g.stores]


def query_sv_stores(g: OrgGraph, sv_name: str) -> list[Store]:
    """SV の管轄店一覧。"""
    codes = g.by_sv.get(sv_name, [])
    return [g.stores[c] for c in codes if c in g.stores]


def query_sv_manager_am(g: OrgGraph, sv_name: str) -> str | None:
    """SV の上司 (AM)。"""
    return g.sv_to_am.get(sv_name)


def query_prefecture_ams(g: OrgGraph, prefecture: str) -> set[str]:
    """都道府県を担当する全 AM (複数の場合あり)。"""
    codes = g.by_prefecture.get(prefecture, [])
    return {g.stores[c].am for c in codes if c in g.stores and g.stores[c].am}


def query_am_subordinate_svs(g: OrgGraph, am_name: str) -> set[str]:
    """AM の配下 SV 一覧 (1-hop)。"""
    return g.am_to_svs.get(am_name, set())


# ─── bi-temporal multi-hop ───────────────────
def query_diff_am_stores(g_from: OrgGraph, g_to: OrgGraph, am_name: str) -> dict:
    """ある AM が **時期 A で管轄してたが、時期 B では別 AM になった店** を返す。

    g_from → g_to の間で AM が変わった store だけ集める。
    """
    from_codes = set(g_from.by_am.get(am_name, []))
    moved_away: list[dict] = []   # その AM 配下 → 他 AM 配下になった
    still_have: list[int] = []    # 引き続き同じ AM 配下
    for code in from_codes:
        if code not in g_to.stores:
            # 閉店 / 削除
            moved_away.append({"code": code, "from": g_from.stores[code].name,
                               "to_am": "(店舗削除)"})
            continue
        new_am = g_to.stores[code].am
        if new_am != am_name:
            moved_away.append({
                "code": code,
                "name": g_from.stores[code].name,
                "to_am": new_am,
            })
        else:
            still_have.append(code)
    return {
        "am": am_name,
        "snapshot_from": g_from.snapshot_date,
        "snapshot_to": g_to.snapshot_date,
        "n_in_from": len(from_codes),
        "n_still_in_to": len(still_have),
        "n_moved_away": len(moved_away),
        "moved_away": moved_away[:50],  # 上位 50 件
    }


def query_sv_am_history(g_list: list[OrgGraph], sv_name: str) -> list[dict]:
    """SV が過去 snapshot を通して、上司 AM がいつ変わったかの履歴。"""
    history = []
    last_am = None
    for g in g_list:
        am = g.sv_to_am.get(sv_name)
        if am != last_am:
            history.append({"snapshot_date": g.snapshot_date, "am": am})
            last_am = am
    return history


# ─── CLI ─────────────────────────────────────
def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--list-snapshots", action="store_true")
    ap.add_argument("--build", help="snapshot date (YYYY-MM-DD) を build")
    ap.add_argument("--query", choices=[
        "am-stores", "sv-stores", "sv-am", "am-svs",
        "prefecture-ams", "diff-am-stores", "sv-am-history",
    ], help="クエリ種別")
    ap.add_argument("--name", help="AM / SV 名 (--query 系)")
    ap.add_argument("--from", dest="date_from", help="--query diff-* の起点 snapshot")
    ap.add_argument("--to", dest="date_to", help="--query diff-* の終点 snapshot")
    args = ap.parse_args()

    if args.list_snapshots:
        for s in list_snapshots():
            print(s)
        return 0

    if args.build:
        g = load_snapshot(args.build)
        print(json.dumps({
            "snapshot_date": g.snapshot_date,
            "valid_from": g.valid_from,
            "valid_to": g.valid_to,
            "n_stores": len(g.stores),
            "n_ams": len(g.by_am),
            "n_svs": len(g.by_sv),
            "n_prefectures": len(g.by_prefecture),
        }, ensure_ascii=False, indent=2))
        return 0

    if args.query:
        if args.query == "diff-am-stores":
            if not (args.date_from and args.date_to and args.name):
                print("--from / --to / --name 必要")
                return 1
            g_from = load_snapshot(args.date_from)
            g_to = load_snapshot(args.date_to)
            result = query_diff_am_stores(g_from, g_to, args.name)
            print(json.dumps(result, ensure_ascii=False, indent=2))
            return 0

        if args.query == "sv-am-history":
            snaps = [load_snapshot(s) for s in list_snapshots()]
            history = query_sv_am_history(snaps, args.name)
            print(json.dumps(history, ensure_ascii=False, indent=2))
            return 0

        # 単一 snapshot クエリ (最新を使う)
        snaps = list_snapshots()
        if not snaps:
            print("no snapshots")
            return 1
        g = load_snapshot(snaps[-1])
        if args.query == "am-stores":
            stores = query_am_stores(g, args.name)
            print(json.dumps([{
                "code": s.code, "name": s.name, "prefecture": s.prefecture,
                "sv": s.sv, "manager": s.manager,
            } for s in stores], ensure_ascii=False, indent=2))
        elif args.query == "sv-stores":
            stores = query_sv_stores(g, args.name)
            print(json.dumps([{
                "code": s.code, "name": s.name, "manager": s.manager,
            } for s in stores], ensure_ascii=False, indent=2))
        elif args.query == "sv-am":
            am = query_sv_manager_am(g, args.name)
            print(json.dumps({"sv": args.name, "am": am}, ensure_ascii=False))
        elif args.query == "am-svs":
            svs = query_am_subordinate_svs(g, args.name)
            print(json.dumps(sorted(svs), ensure_ascii=False))
        elif args.query == "prefecture-ams":
            ams = query_prefecture_ams(g, args.name)
            print(json.dumps(sorted(ams), ensure_ascii=False))
        return 0

    ap.print_help()
    return 0


if __name__ == "__main__":
    sys.exit(main())
