"""
brain_graph.py — Wiki の Markdown 集合を nodes / edges に変換

設計方針:
  ノード = wiki/*.md 1 ファイル 1 ノード
  各ノードに「思考度スコア」を計算 (recency × centrality × depth)
    - recency: updated frontmatter（無ければ file mtime）の指数減衰（半減期60日）
    - centrality: 被参照数 (in-degree)
    - depth: ファイルサイズ（log scale, 文書の厚み）
  surface_pct（デフォ40%）に入らないノードは
    カテゴリごとの「💭 思考のストレージ」ハブにまとめて隠す
  ハブをクリックすると hidden_nodes が sidebar に展開される

URL パラメタ:
  ?surface=40   surface に表示するノードの割合（%）。デフォ40
  ?all=1        ストレージにまとめず全ノード表示

vis-network 互換 JSON を返す。
"""

from __future__ import annotations

import datetime
import math
import os
import re
import time
from collections import defaultdict
from pathlib import Path
from typing import Any

WIKI_LINK_RE = re.compile(r"\[\[([^\]|]+)(?:\|[^\]]+)?\]\]")


# 1階層目のディレクトリで色分け
# ★2026-06-01「脳味噌っぽく」→ ★2026-06-20 海山指示「もう少し燻んで・実際の脳っぽく(今はチープ)」:
#   ネオン発光を捨て、解剖学的に燻んだ低彩度パレットへ。暗い tissue 背景の上で細胞が鈍く発光する
#   落ち着いたトーン(彩度・輝度を下げ、加算発光も弱める)。
#   core      → dusty rose/garnet (hub ニューロン)
#   knowledge → muted steel-blue
#   people    → muted sage
#   projects  → dusty mauve
#   decisions → muted ochre
GROUP_COLORS = {
    "core":       {"bg": "#A85A6B", "bd": "#6E3A48"},
    "knowledge":  {"bg": "#5A86A0", "bd": "#3A5A70"},
    "people":     {"bg": "#5E987F", "bd": "#3D6B58"},
    "projects":   {"bg": "#8576A4", "bd": "#564A78"},
    "decisions":  {"bg": "#B89760", "bd": "#856A3E"},
    "misc":       {"bg": "#7E8597", "bd": "#545b69"},
    "storage":    {"bg": "#1c1822", "bd": "#3a3450"},
}

CORE_FILES = {"identity.md", "style.md", "thinking.md", "index.md"}

# 半減期: 60日（updated から60日経つと recency が 0.5 になる）
RECENCY_HALF_LIFE_DAYS = 60.0
# サーフェスに出すノードの割合（デフォ）
DEFAULT_SURFACE_PCT = 40
# 各グループ最低何ノード見せるか（小グループ保護）
MIN_PER_GROUP = 3
# core は常に全部 surface
ALWAYS_SURFACE_GROUPS = {"core"}


# ─── frontmatter / parser ───

def _parse_frontmatter(text: str) -> dict:
    meta: dict[str, Any] = {
        "updated": "",
        "confidence": "",
        "tags": [],
        "sources": [],
    }
    if not text.startswith("---"):
        return meta
    try:
        end = text.index("\n---", 3)
    except ValueError:
        return meta
    fm = text[3:end]
    for line in fm.splitlines():
        line = line.strip()
        if not line or ":" not in line:
            continue
        k, v = line.split(":", 1)
        k, v = k.strip(), v.strip()
        if k in ("tags", "sources"):
            items = [i.strip() for i in v.strip("[]").split(",") if i.strip()]
            meta[k] = items
        elif k in ("updated", "confidence"):
            meta[k] = v
    return meta


def _extract_title(text: str, default: str) -> str:
    for line in text.splitlines():
        if line.startswith("# "):
            return line[2:].strip()
    return default


def _group_for(rel: Path) -> str:
    if rel.name in CORE_FILES and len(rel.parts) == 1:
        return "core"
    if len(rel.parts) >= 2:
        return rel.parts[0]
    return "misc"


def _normalize_key(s: str) -> str:
    s = s.strip().lower()
    if s.endswith(".md"):
        s = s[:-3]
    return s


# ─── recency / score ───

def _parse_date(s: str) -> datetime.datetime | None:
    if not s:
        return None
    s = s.strip().strip('"\'')
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%dT%H:%M:%S", "%Y-%m-%d", "%Y/%m/%d"):
        try:
            return datetime.datetime.strptime(s, fmt)
        except ValueError:
            continue
    try:
        return datetime.datetime.fromisoformat(s.replace("Z", ""))
    except Exception:
        return None


def _recency_factor(days_old: float, half_life: float = RECENCY_HALF_LIFE_DAYS) -> float:
    """指数減衰: 0日=1.0, 半減期=0.5, 半減期×3=0.125"""
    if days_old < 0:
        days_old = 0
    return math.exp(-math.log(2) * days_old / half_life)


def _vis_color(col: dict, recency: float) -> dict:
    """palette entry ({bg, bd}) + recency → vis-network color dict。

    ★2026-07-05: 分類色と記憶層色で同一の dim curve を共有する (二重定義は
    片側だけ調整されて配色モード間で明度がズレる drift の元 = code-review reuse-3)。
    """
    f = 0.4 + 0.6 * recency
    return {
        "background": _dim_color(col["bg"], f),
        "border": _dim_color(col["bd"], f),
        "highlight": {"background": col["bg"], "border": "#ffffff"},
        "hover":     {"background": col["bg"], "border": "#ffffff"},
    }


def _dim_color(hex_color: str, factor: float, bg: str = "#1a1a1a") -> str:
    """背景色に向かって blend して暗くする。factor=1.0 で原色、0.0 で背景と同色"""
    factor = max(0.25, min(1.0, factor))  # 最低でも 25% は元の色を保つ
    hex_color = hex_color.lstrip("#")
    bg = bg.lstrip("#")
    r, g, b = int(hex_color[0:2], 16), int(hex_color[2:4], 16), int(hex_color[4:6], 16)
    br, bg_, bb = int(bg[0:2], 16), int(bg[2:4], 16), int(bg[4:6], 16)
    nr = int(br + (r - br) * factor)
    ng = int(bg_ + (g - bg_) * factor)
    nb = int(bb + (b - bb) * factor)
    return f"#{nr:02x}{ng:02x}{nb:02x}"


# ─── main ───

def build_graph_data(
    wiki_dir: Path,
    surface_pct: int = DEFAULT_SURFACE_PCT,
    show_all: bool = False,
    min_size: int = 0,
    admin: bool = False,
) -> dict:
    """Wiki ディレクトリをスキャンしてノード/エッジを生成

    Args:
        wiki_dir: WIKI_DIR
        surface_pct: 表に出すノードの割合（%）。残りはストレージへ
        show_all: True ならストレージに入れず全部表示
        min_size: この byte 数未満は最初から除外
        admin: Brain Map の admin tier (/api/brain/graph が BRAIN_EXTENSION_KEY で
            認証された時) のみ True。True で全ノードを出す = deep-private
            (personal/ + interview/、path 防御) **と** clone_visibility: private
            (法務/人事 decision 等、frontmatter 防御) の両方をノード化。
            ★2026-07-11 海山指示「Brain Map は個人利用だから全部見れる」。
            **デフォルト False** = 公開/非 admin 経路 (弱い token tier)・既存テストは
            深層 private も clone_visibility: private も build 段階で除外
            (title/tags/path すら graph JSON に出さない = operator endpoint #2 と一貫)。
            ★元 param 名 include_deep_private から改名 — private も制御するようになり
            名前が乖離するため (§1.15 adversarial 検証で surfaced した token tier の
            private title/meta 露出穴を封鎖、b60f6b1 の adversarial 検証)。
    """
    surface_pct = max(5, min(100, surface_pct))

    key_to_path: dict[str, str] = {}
    node_info: dict[str, dict] = {}
    now = datetime.datetime.now()

    # ─── 1. ノード収集 + recency 計算 ───
    from brain_wiki_helpers.domain import is_deep_private_rel
    from brain_wiki_helpers.visibility import parse_clone_visibility
    # ★2026-07-05 記憶層オントロジー Phase 0 (ADR 2026-07-05-wiki-ontology-multilayer)。
    #   関数内 import は既存 domain と同じ流儀 = brain_graph の import-light 契約を維持
    #   (module top で brain_wiki_helpers/__init__ 連鎖を引かない、code-review C-1)
    from brain_wiki_helpers.ontology import (
        LAYER_COLORS, LAYER_LABELS, LAYERS, layer_of, node_kind_of,
    )
    for md in wiki_dir.rglob("*.md"):
        # .obsidian 配下を除外
        try:
            rel = md.relative_to(wiki_dir)
        except ValueError:
            continue
        if any(p.startswith(".") for p in rel.parts):
            continue
        # ★2026-06-28 personal / ★2026-07-03 interview (v3 ADR DA R6): 知識グラフ (/api/brain/graph)
        #   に深層 private を出さない (DA cross-check: graph viz は web UI 露出、node title が見えてしまう)
        #   ★2026-07-11 海山指示: admin tier (admin=True) の Brain Map だけ例外的に出す。
        #   デフォルト False = 公開/非 admin/既存テストは従来どおり除外 (title も出さない)。
        if is_deep_private_rel(rel) and not admin:
            continue

        try:
            content = md.read_text(encoding="utf-8", errors="replace")
        except Exception:
            continue
        if len(content) < min_size:
            continue
        # ★2026-07-11 (b60f6b1 の §1.15 adversarial 検証で surfaced): token tier は
        #   clone_visibility: private (法務/人事 decision 等) も build 段階で除外する。
        #   graph は path 防御 (上) だけで frontmatter private を素通りしていたため、弱い token でも
        #   private ノードの title/tags/path が JSON 露出していた (本文は /api/brain/wiki が #2 で
        #   404 だが meta は見えた)。判定は sibling operator endpoint と同じ == "private"
        #   (語彙は public/private の二値)。fail-safe: parse_clone_visibility は frontmatter 無し /
        #   clone_visibility 未設定を private 扱いにするため build から落ちる。
        #   admin tier (admin=True) は従来どおり全開 (private も deep-private も出す)。
        if not admin and parse_clone_visibility(content) == "private":
            continue

        rel_str = str(rel)
        meta = _parse_frontmatter(content)
        title = _extract_title(content, rel.stem)
        group = _group_for(rel)

        # recency: updated フィールドを優先、無ければ mtime
        updated_dt = _parse_date(meta["updated"])
        if updated_dt is None:
            try:
                mtime = md.stat().st_mtime
                updated_dt = datetime.datetime.fromtimestamp(mtime)
            except Exception:
                updated_dt = now - datetime.timedelta(days=365)
        days_old = max(0.0, (now - updated_dt).total_seconds() / 86400)
        recency = _recency_factor(days_old)

        keys = {
            _normalize_key(rel_str),
            _normalize_key(rel_str.replace(".md", "")),
            _normalize_key(rel.stem),
            _normalize_key(title),
        }
        for k in keys:
            if k and k not in key_to_path:
                key_to_path[k] = rel_str

        node_info[rel_str] = {
            "id": rel_str,
            "label": title[:40],
            "group": group,
            "layer": layer_of(rel),
            "kind": node_kind_of(rel),
            "path": rel_str,
            "title": title,
            "tags": meta["tags"],
            "updated": meta["updated"] or updated_dt.strftime("%Y-%m-%d"),
            "confidence": meta["confidence"],
            "sources": meta["sources"],
            "size": len(content),
            "days_old": int(days_old),
            "recency": round(recency, 4),
            "in_degree": 0,
            "out_degree": 0,
            "is_storage": False,
        }

    # ─── 2. エッジ抽出 + in/out-degree カウント ───
    edges: list[dict] = []
    seen_edges: set[tuple[str, str]] = set()

    for rel_str, info in node_info.items():
        md = wiki_dir / rel_str
        try:
            content = md.read_text(encoding="utf-8", errors="replace")
        except Exception:
            continue
        for m in WIKI_LINK_RE.finditer(content):
            target = _normalize_key(m.group(1))
            dst = key_to_path.get(target)
            if not dst or dst == rel_str:
                continue
            key = (rel_str, dst)
            if key in seen_edges:
                continue
            seen_edges.add(key)
            edges.append({"from": rel_str, "to": dst, "type": "link"})
            node_info[rel_str]["out_degree"] += 1
            node_info[dst]["in_degree"] += 1

    # ─── 3. 共通タグエッジ（軽量）───
    tag_to_nodes: dict[str, list[str]] = defaultdict(list)
    for rel_str, info in node_info.items():
        for tag in info["tags"]:
            tag_to_nodes[tag].append(rel_str)
    for tag, nodes in tag_to_nodes.items():
        if len(nodes) < 2 or len(nodes) > 30:
            continue
        hub = nodes[0]
        for other in nodes[1:]:
            pair = tuple(sorted([hub, other]))
            if pair in seen_edges:
                continue
            seen_edges.add(pair)
            edges.append({
                "from": hub, "to": other,
                "type": "tag", "label": tag, "dashes": True,
            })

    # ─── 3.5 承認済み bridge エッジ (★2026-07-05 Phase 1: 孤島接続) ───
    # 実測シグナル (共起/embedding) から提案され海山が /bridge で承認したエッジのみ。
    # sidecar (data/brain/graph/edges.jsonl) 由来 = frontmatter 不使用 (ADR §4)。
    # 深層 private は node walk 段階で除外済 → node_info に無い endpoint は自然に落ちる。
    bridge_nodes: set[str] = set()   # 承認エッジの両端 = 海山キュレーション済 → 常に surface
    try:
        from brain_wiki_helpers.edge_store import load_approved_edges
        for be in load_approved_edges(wiki_dir.parent):
            f, t = be.get("from"), be.get("to")
            if f not in node_info or t not in node_info:
                continue
            pk = tuple(sorted((f, t)))
            if (f, t) in seen_edges or (t, f) in seen_edges or pk in seen_edges:
                continue
            seen_edges.add(pk)
            rel_label = be.get("relation", "")
            e = {"from": f, "to": t, "type": "bridge"}
            if rel_label and rel_label != "related":
                e["label"] = rel_label
            edges.append(e)
            node_info[f]["out_degree"] += 1
            node_info[t]["in_degree"] += 1
            bridge_nodes.update((f, t))
    except Exception:
        pass   # sidecar 不在/破損で graph 全体は落とさない

    # ─── 4. スコア計算 ───
    # score = recency × (1 + 0.4 × in_degree) × √(size / 1000 + 0.5)
    # in_degree: 良く参照されるほど大きく
    # size: 内容の厚みを log で軽く効かせる
    for n in node_info.values():
        centrality = 1.0 + 0.4 * n["in_degree"]
        depth = math.sqrt(n["size"] / 1000.0 + 0.5)
        n["score"] = round(n["recency"] * centrality * depth, 4)

    # ─── 5. surface / storage 判定 ───
    nodes_sorted = sorted(node_info.values(), key=lambda x: x["score"], reverse=True)

    if show_all:
        surface_ids: set[str] = set(n["id"] for n in nodes_sorted)
    else:
        # トップ surface_pct% を採用
        cutoff = max(1, int(len(nodes_sorted) * surface_pct / 100))
        surface_ids = set(n["id"] for n in nodes_sorted[:cutoff])

        # 各グループ最低 MIN_PER_GROUP は出す（小カテゴリ保護）
        by_group: dict[str, list[dict]] = defaultdict(list)
        for n in nodes_sorted:
            by_group[n["group"]].append(n)
        for g, gns in by_group.items():
            for n in gns[:MIN_PER_GROUP]:
                surface_ids.add(n["id"])

        # core は常に全部
        for n in node_info.values():
            if n["group"] in ALWAYS_SURFACE_GROUPS:
                surface_ids.add(n["id"])

        # ★2026-07-05 承認済み bridge の両端も常に surface — 海山が明示承認した接続は
        #   score より強いキュレーション信号 (沈めると「承認したのに見えない」= 実測 17/32)
        surface_ids |= bridge_nodes

    # ─── 6. visual properties (color brightness, value) ───
    # 全ノードのスコアレンジを把握 → value にマッピング
    surface_nodes = [n for n in node_info.values() if n["id"] in surface_ids]
    # ★2026-07-05 index (ハブ骨格) は正規化レンジから除外 — 外れ値 score (実測: index 133 vs
    #   2位 19.6) が max を支配すると残り全ノードが value 12-21 に圧縮されラベルが全滅する
    #   (code-review altitude-1 で実測確認)。減衰は norm 段階で行い value/font/ラベル可視性を
    #   一括で追従させる (ADR §3 Phase 0 の表示側減衰)。
    rng_nodes = [n for n in surface_nodes if n["kind"] != "index"] or surface_nodes
    if rng_nodes:
        # ★2026-07-05 本番実測 (surface 621): score は min 1.2 / p50 2.3 / p95 8.6 / max 780
        #   (thinking.md in-link 283 等の core メガハブ)。素の min-max だと外れ値が
        #   レンジを支配し大半が下限に張り付く → p95 で winsorize してサイズを分布させる
        #   (小集合では winsorize 不要 = max のまま)。
        _sorted = sorted(n["score"] for n in rng_nodes)
        min_score = _sorted[0]
        if len(_sorted) >= 20:
            max_score = _sorted[int(0.95 * (len(_sorted) - 1))]
        else:
            max_score = _sorted[-1]
    else:
        max_score = min_score = 1.0
    score_range = max(0.001, max_score - min_score)
    _INDEX_NORM_CAP = 0.26   # ≒ value 29 (< 30): index はハブ骨格なので小さく

    # ★ラベルは閾値でなく rank で決める: 本番の score 分布 (p50 が p95 の 27%) では
    #   閾値方式は全滅 (6/621) か全表示 (326/621) に振れる。上位 10% (最低 20) + core が安定
    _label_k = max(20, len(surface_nodes) // 10)
    _labeled_ids = {
        n["id"] for n in sorted(surface_nodes, key=lambda x: x["score"], reverse=True)[:_label_k]
    }

    for n in surface_nodes:
        # value: 12 .. 80 にマップ（スコアに対して非線形）
        norm = (n["score"] - min_score) / score_range
        norm = max(0.0, min(1.0, norm))
        if n["kind"] == "index":
            norm = min(norm, _INDEX_NORM_CAP)
        n["value"] = int(12 + 68 * norm)

        # ラベル可視性: rank 上位 + core のみ（クラッタ削減）
        if n["id"] not in _labeled_ids and n["group"] != "core":
            n["label_full"] = n["label"]
            n["label"] = ""

        g = n["group"]
        # recency に応じて色を暗く（古いものは沈める）
        n["color"] = _vis_color(GROUP_COLORS.get(g, GROUP_COLORS["misc"]), n["recency"])
        # ★2026-07-05 記憶層配色 (client 側トグルで group ⇔ layer を切替)。
        #   LAYER_COLORS は直接 index = 層の追加漏れは初回 build で loud に落とす
        #   (get + misc fallback は taxonomy バグを灰色ノードに隠す、code-review altitude-4)
        n["color_layer"] = _vis_color(LAYER_COLORS[n["layer"]], n["recency"])
        # フォントサイズもスコアに連動
        n["font"] = {
            "color": "#ffffff" if n["recency"] > 0.4 else "#cccccc",
            "size": int(11 + 6 * norm),
            "face": "sans-serif",
            "strokeWidth": 2,
            "strokeColor": "#111111",
        }

    # ─── 7. ストレージハブ生成 ───
    storage_hubs: list[dict] = []
    storage_payloads: list[dict] = []  # フロント側で表示するための詳細
    if not show_all:
        storage_by_group: dict[str, list[dict]] = defaultdict(list)
        for n in node_info.values():
            if n["id"] not in surface_ids:
                storage_by_group[n["group"]].append(n)

        for g, hidden in storage_by_group.items():
            if not hidden:
                continue
            hub_id = f"__storage__{g}"
            hidden_sorted = sorted(hidden, key=lambda x: x["score"], reverse=True)
            payload = [
                {
                    "id": h["id"],
                    "title": h["title"],
                    "path": h["path"],
                    "size": h["size"],
                    "days_old": h["days_old"],
                    "score": h["score"],
                    "in_degree": h["in_degree"],
                }
                for h in hidden_sorted
            ]
            col = GROUP_COLORS["storage"]
            # ★2026-07-05 hub にも記憶層の縁色を持たせる — layer モードで group 縁色のままだと
            #   凡例と矛盾する (ochre 縁 = 凡例では「エピソード」だが中身は decisions、review B-1)
            hub_layer = layer_of(f"{g}/_.md")
            storage_hubs.append({
                "id": hub_id,
                "label": f"💭 {g} ({len(hidden)})",
                "group": "storage",
                "layer": hub_layer,
                "is_storage": True,
                "storage_group": g,
                "hidden_count": len(hidden),
                "value": min(60, 25 + len(hidden) // 4),
                "shape": "diamond",
                "color": {
                    "background": col["bg"],
                    "border": GROUP_COLORS.get(g, GROUP_COLORS["misc"])["bg"],
                    "highlight": {"background": "#3a3a3a", "border": "#ffffff"},
                    "hover":     {"background": "#3a3a3a", "border": "#ffffff"},
                },
                "color_layer": {
                    "background": col["bg"],
                    "border": LAYER_COLORS[hub_layer]["bg"],
                    "highlight": {"background": "#3a3a3a", "border": "#ffffff"},
                    "hover":     {"background": "#3a3a3a", "border": "#ffffff"},
                },
                "borderWidth": 2,
                "borderWidthSelected": 3,
                "font": {
                    "color": "#cccccc",
                    "size": 13,
                    "face": "sans-serif",
                    "strokeWidth": 2,
                    "strokeColor": "#000000",
                },
            })
            storage_payloads.append({"hub_id": hub_id, "items": payload})

    # ─── 8. エッジ再構成: ストレージノード行きはハブにリダイレクト ───
    new_edges: list[dict] = []
    edge_seen: set[tuple[str, str]] = set()
    for e in edges:
        f, t = e["from"], e["to"]
        f_in_surface = f in surface_ids
        t_in_surface = t in surface_ids

        # storage to storage は省略（ハブ間も繋がない、ノイズになるだけ）
        if not f_in_surface and not t_in_surface:
            continue

        # ★2026-07-05 bridge エッジは実 endpoint 間の関係 — ハブ (集合体) に redirect すると
        #   「A —evidence_for→ storage束」という誤帰属描画になる → surface 同士の時だけ描く
        #   (bridge は in_degree を先に押し上げるので対象は surface に浮きやすい、review UX-3)
        if e.get("type") == "bridge" and not (f_in_surface and t_in_surface):
            continue

        # storage 側はハブIDに置き換え
        if not show_all:
            if not f_in_surface:
                f = f"__storage__{node_info[f]['group']}"
            if not t_in_surface:
                t = f"__storage__{node_info[t]['group']}"

        if f == t:
            continue
        ek = (f, t)
        if ek in edge_seen:
            continue
        edge_seen.add(ek)

        edge_obj = {"from": f, "to": t, "type": e.get("type", "link")}
        if e.get("dashes"):
            edge_obj["dashes"] = True
        if e.get("label"):
            edge_obj["label"] = e["label"]
        # storage 行きは薄く
        if f.startswith("__storage__") or t.startswith("__storage__"):
            edge_obj["to_storage"] = True
        new_edges.append(edge_obj)

    # 出力するノードリスト
    output_nodes = surface_nodes + storage_hubs
    # 層別件数は 1 pass で集計 (layer_meta と stats.layers が共有、code-review eff-1)
    from collections import Counter
    _layer_counts = Counter(n["layer"] for n in node_info.values())

    return {
        "nodes": output_nodes,
        "edges": new_edges,
        "storage": storage_payloads,
        # ★2026-07-05 記憶層の凡例メタ (client が layer モードの legend + 件数表示を組む)
        "layer_meta": [
            {"key": L, "label": LAYER_LABELS[L], "bg": LAYER_COLORS[L]["bg"],
             "count": _layer_counts.get(L, 0)}
            for L in LAYERS
        ],
        "stats": {
            "node_count": len(node_info),
            "surface_count": len(surface_nodes),
            "storage_count": sum(s["hidden_count"] for s in storage_hubs),
            "edge_count": len(new_edges),
            "link_edges": sum(1 for e in new_edges if e.get("type") == "link"),
            "tag_edges": sum(1 for e in new_edges if e.get("type") == "tag"),
            "bridge_edges": sum(1 for e in new_edges if e.get("type") == "bridge"),
            "groups": {
                g: sum(1 for n in node_info.values() if n["group"] == g)
                for g in GROUP_COLORS if g != "storage"
            },
            "layers": {L: _layer_counts.get(L, 0) for L in LAYERS},
            "surface_pct": surface_pct,
            "show_all": show_all,
        },
    }


# ─── 埋め込み HTML ───
GRAPH_HTML = r"""<!doctype html>
<html lang="ja">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1,viewport-fit=cover">
<meta name="apple-mobile-web-app-capable" content="yes">
<meta name="theme-color" content="#070512">
<title>Brain Map</title>
<script src="https://unpkg.com/vis-network/standalone/umd/vis-network.min.js"></script>
<style>
  :root { --vh: 100vh; }
  @supports (height: 100dvh) { :root { --vh: 100dvh; } }
  * { box-sizing: border-box; -webkit-tap-highlight-color: transparent; }
  html, body {
    margin:0; padding:0; height:var(--vh); overflow:hidden;
    font-family:-apple-system,BlinkMacSystemFont,"Helvetica Neue",sans-serif;
    background:#070512; color:#eee;
    -webkit-font-smoothing:antialiased;
    overscroll-behavior:none;
  }
  #app { display:flex; height:var(--vh); overflow:hidden; }
  /* ★2026-06-20 燻んで実際の脳っぽく: 青紫ネオンをやめ、暗い tissue 風 warm-charcoal 背景 +
     ごく微かな左右 glow (= 脳の 2 半球を暗示、彩度を落とす) */
  #graph {
    flex:1; min-width:0; min-height:0;
    background:
      radial-gradient(ellipse 62% 72% at 30% 45%, rgba(120,92,108,0.06) 0%, transparent 58%),
      radial-gradient(ellipse 62% 72% at 70% 45%, rgba(84,98,120,0.06) 0%, transparent 58%),
      radial-gradient(ellipse at 50% 48%, #17151c 0%, #100e14 45%, #08070b 100%);
    touch-action:none; position:relative;
  }
  #graph canvas { touch-action:none !important; }

  /* ── Apple design language (★2026-07-12 海山「もっとスタイリッシュに、Apple の哲学の様に」)
     frosted glass + hairline + SF 系タイポ + 抑制されたアクセント (低彩度は維持) ── */
  :root {
    --glass: rgba(28,28,30,0.62);
    --glass-heavy: rgba(24,24,27,0.78);
    --hairline: rgba(255,255,255,0.09);
    --text: #f2f2f4; --text2: #98989d; --text3: #6d6d72;
    --accent: #6E93A8;
    --ease: cubic-bezier(0.25,0.1,0.25,1);
  }
  #sidebar {
    width:380px; background:var(--glass-heavy);
    backdrop-filter:blur(24px) saturate(160%); -webkit-backdrop-filter:blur(24px) saturate(160%);
    border-left:1px solid var(--hairline);
    padding:20px; overflow-y:auto; font-size:14px; line-height:1.55;
    -webkit-overflow-scrolling:touch;
  }
  #close-btn { display:none; }
  .grabber { display:none; }

  @media (max-width:760px) {
    #sidebar {
      position:fixed; left:0; right:0; bottom:0;
      width:100%; height:72vh; height:72dvh;
      max-height:72vh; max-height:72dvh;
      transform:translateY(100%);
      transition:transform .32s var(--ease);
      border-left:none; border-top:1px solid var(--hairline);
      border-radius:18px 18px 0 0;
      box-shadow:0 -16px 48px rgba(0,0,0,.55);
      padding:10px 18px calc(20px + env(safe-area-inset-bottom));
      z-index:10;
    }
    #sidebar.open { transform:translateY(0); }
    .grabber { display:block; width:36px; height:5px; border-radius:3px;
      background:rgba(255,255,255,0.22); margin:2px auto 12px; }
    #close-btn {
      display:flex; align-items:center; justify-content:center;
      position:absolute; top:12px; right:14px;
      background:rgba(255,255,255,0.08); border:none; color:#c7c7cc;
      font-size:17px; width:30px; height:30px; border-radius:50%;
      line-height:1; cursor:pointer;
    }
    #sidebar h2 { padding-right:36px; }
  }

  h2 { font-size:17px; margin:0 0 8px; color:var(--text); font-weight:600;
    letter-spacing:-0.022em; text-wrap:balance; }
  .meta { color:var(--text2); font-size:12px; margin-bottom:8px; word-break:break-all; }
  .tag { display:inline-block; background:rgba(255,255,255,0.08); color:#d1d1d6;
    padding:3px 10px; border-radius:999px; margin:2px; font-size:11px;
    border:1px solid rgba(255,255,255,0.05); }

  .legend {
    position:absolute; top:14px; left:14px; z-index:5;
    background:var(--glass); backdrop-filter:blur(18px) saturate(150%);
    -webkit-backdrop-filter:blur(18px) saturate(150%);
    padding:9px 14px; border-radius:13px; font-size:11px;
    pointer-events:none; line-height:1.8;
    border:1px solid var(--hairline);
    box-shadow:0 8px 32px rgba(0,0,0,0.35);
    color:#d1d1d6;
  }
  .legend .sw { display:inline-block; width:9px; height:9px;
    border-radius:50%; margin-right:7px; vertical-align:middle; }
  .legend .sw.diamond { border-radius:2px; transform:rotate(45deg); }
  @media (max-width:760px) {
    .legend { font-size:10px; padding:6px 9px; line-height:1; border-radius:11px; }
    .legend .row { display:inline-block; margin-right:8px; vertical-align:middle; }
    .legend .label { display:none; }
  }

  #toolbar {
    position:absolute; top:14px; right:14px; z-index:5;
    display:flex; gap:8px; align-items:center; max-width:calc(100% - 28px);
    background:var(--glass); backdrop-filter:blur(18px) saturate(150%);
    -webkit-backdrop-filter:blur(18px) saturate(150%);
    padding:6px; border-radius:14px;
    border:1px solid var(--hairline);
    box-shadow:0 8px 32px rgba(0,0,0,0.35);
  }
  #toolbar input, #toolbar select, #toolbar button {
    background:rgba(255,255,255,0.07);
    border:none; color:var(--text);
    padding:7px 12px; border-radius:9px;
    font-size:16px; min-height:34px;
    font-family:inherit;
    -webkit-appearance:none; appearance:none;
    cursor:pointer; outline:none;
    transition:background .2s var(--ease);
  }
  #toolbar input { width:150px; }
  #toolbar input::placeholder { color:var(--text3); }
  #toolbar input:focus { background:rgba(255,255,255,0.12); }
  #toolbar select:hover, #toolbar button:hover { background:rgba(255,255,255,0.13); }
  #toolbar button { padding:7px 11px; font-size:13px; min-width:38px; font-weight:500; }
  #toolbar button.active { background:var(--accent); color:#fff; }
  /* segmented control (分類 / 記憶層) — Apple 風 */
  .seg { display:flex; background:rgba(255,255,255,0.07); border-radius:9px; padding:2px; gap:2px; }
  .seg button {
    background:transparent !important; border:none; color:var(--text2) !important;
    min-height:30px !important; padding:5px 12px !important; border-radius:7px !important;
    font-size:12.5px !important; font-weight:500;
    transition:all .2s var(--ease);
  }
  .seg button.on {
    background:rgba(255,255,255,0.16) !important; color:var(--text) !important;
    box-shadow:0 1px 4px rgba(0,0,0,0.3);
  }
  @media (max-width:760px) {
    #toolbar { top:auto; bottom:14px; right:14px; left:14px; }
    #toolbar input { flex:1; width:auto; min-width:0; }
    .seg button { padding:5px 9px !important; }
  }

  #stats { color:var(--text3); font-size:11px; margin-top:16px; padding-top:12px;
    border-top:1px solid var(--hairline); line-height:1.7; }

  pre { white-space:pre-wrap; word-break:break-all;
    background:rgba(0,0,0,0.35); padding:14px; border-radius:12px;
    font-size:12px; line-height:1.6; max-height:50vh; overflow:auto;
    border:1px solid rgba(255,255,255,0.06);
    -webkit-overflow-scrolling:touch;
  }

  /* ストレージリスト — Apple grouped list */
  .storage-list { margin-top:12px; background:rgba(255,255,255,0.05);
    border-radius:12px; overflow:hidden; border:1px solid rgba(255,255,255,0.05); }
  .storage-item {
    display:flex; align-items:baseline; gap:8px;
    padding:10px 12px; cursor:pointer;
    border-bottom:1px solid rgba(255,255,255,0.05);
    transition:background .18s var(--ease);
    -webkit-tap-highlight-color: rgba(110,147,168,.18);
  }
  .storage-item:last-child { border-bottom:none; }
  .storage-item:hover { background:rgba(255,255,255,0.08); }
  .storage-item .title { flex:1; color:#e5e5ea; font-size:13px; min-width:0; overflow:hidden; text-overflow:ellipsis; white-space:nowrap; }
  .storage-item .age { color:var(--text3); font-size:11px; flex-shrink:0; }
  .storage-item .badge { color:var(--text2); font-size:10px; background:rgba(0,0,0,0.3); padding:2px 7px; border-radius:999px; flex-shrink:0; }
  .storage-item::after { content:'›'; color:var(--text3); font-size:14px; flex-shrink:0; }

  #loading {
    position:absolute; top:50%; left:50%; transform:translate(-50%,-50%);
    color:var(--text2); font-size:13px; z-index:4; text-align:center;
    letter-spacing:0.01em;
  }
  #loading .spinner {
    border:2px solid rgba(255,255,255,0.12); border-top-color:var(--accent);
    width:32px; height:32px; border-radius:50%;
    margin:0 auto 12px; animation:spin .8s linear infinite;
  }
  @keyframes spin { to { transform:rotate(360deg); } }

  #error { color:#ff6961; padding:14px; }
  .back-btn {
    display:inline-flex; align-items:center; color:var(--accent); cursor:pointer; margin-bottom:10px;
    font-size:12px; padding:5px 12px; border-radius:999px;
    background:rgba(110,147,168,0.12);
    transition:background .2s var(--ease);
  }
  .back-btn:hover { background:rgba(110,147,168,0.22); }
  .hint { color:var(--text3); font-size:12px; line-height:1.7; }
</style>
</head>
<body>
<div id="app">
  <div id="graph">
    <div id="loading"><div class="spinner"></div>Loading…</div>
    <div class="legend" id="legend">
      <div class="row"><span class="sw" style="background:#A85A6B;box-shadow:0 0 5px rgba(168,90,107,0.6)"></span><span class="label"> core</span></div>
      <div class="row"><span class="sw" style="background:#5A86A0;box-shadow:0 0 5px rgba(90,134,160,0.6)"></span><span class="label"> knowledge</span></div>
      <div class="row"><span class="sw" style="background:#5E987F;box-shadow:0 0 5px rgba(94,152,127,0.6)"></span><span class="label"> people</span></div>
      <div class="row"><span class="sw" style="background:#8576A4;box-shadow:0 0 5px rgba(133,118,164,0.6)"></span><span class="label"> projects</span></div>
      <div class="row"><span class="sw" style="background:#B89760;box-shadow:0 0 5px rgba(184,151,96,0.6)"></span><span class="label"> decisions</span></div>
      <div class="row"><span class="sw diamond" style="background:#241d3a;border:1px solid #4a3f6b"></span><span class="label"> 思考のストレージ</span></div>
    </div>
    <div id="toolbar">
      <input id="search" placeholder="検索" autocomplete="off" autocapitalize="off" />
      <div class="seg" id="colormode-seg">
        <button data-v="group" class="on">分類</button>
        <button data-v="layer">記憶層</button>
      </div>
      <select id="colormode" title="配色: 分類 / 記憶層" autocomplete="off" hidden>
        <option value="group">分類</option>
        <option value="layer">記憶層</option>
      </select>
      <select id="filter">
        <option value="">全</option>
        <option value="core">core</option>
        <option value="knowledge">knowledge</option>
        <option value="people">people</option>
        <option value="projects">projects</option>
        <option value="decisions">decisions</option>
      </select>
      <button id="toggle-all" title="ストレージを展開して全表示">All</button>
    </div>
  </div>
  <div id="sidebar">
    <div class="grabber"></div>
    <button id="close-btn" aria-label="閉じる">✕</button>
    <h2>Brain Map</h2>
    <div class="meta">
      ノードサイズ = 思考度 (recency × 被参照 × 厚み) · くすみ = 古さ
    </div>
    <div id="detail">
      <div class="hint">ノードをタップすると、その繋がりだけが浮かび上がり、シナプスを信号が流れます。<br>空白をタップで全体に戻る。</div>
    </div>
    <div id="stats"></div>
  </div>
</div>
<script>
function setVH() {
  document.documentElement.style.setProperty('--vh', window.innerHeight + 'px');
}
setVH();
window.addEventListener('resize', setVH);
window.addEventListener('orientationchange', () => setTimeout(setVH, 200));

const qs = new URLSearchParams(location.search);
// ★2026-05-26: ?token=... (= VOICE_ALIGN_TOKEN、dashboard 統一) も accept
const KEY = qs.get('key') || '';
const TOKEN = qs.get('token') || '';
let SHOW_ALL = qs.get('all') === '1';
const SURFACE_PCT = parseInt(qs.get('surface') || '40', 10);

function buildApiUrl() {
  const params = new URLSearchParams();
  if (KEY) params.set('key', KEY);
  if (TOKEN) params.set('token', TOKEN);
  if (SHOW_ALL) params.set('all', '1');
  if (SURFACE_PCT !== 40) params.set('surface', SURFACE_PCT);
  return '/api/brain/graph?' + params.toString();
}
const wikiApi = (path) => {
  let url = `/api/brain/wiki?path=${encodeURIComponent(path)}`;
  if (KEY) url += '&key=' + encodeURIComponent(KEY);
  if (TOKEN) url += '&token=' + encodeURIComponent(TOKEN);
  return url;
};

const detail = document.getElementById('detail');
const statsEl = document.getElementById('stats');
const sidebar = document.getElementById('sidebar');
const loading = document.getElementById('loading');
const isMobile = window.matchMedia('(max-width:760px)').matches;
const toggleAllBtn = document.getElementById('toggle-all');

if (SHOW_ALL) toggleAllBtn.classList.add('active');
toggleAllBtn.addEventListener('click', () => {
  SHOW_ALL = !SHOW_ALL;
  if (SHOW_ALL) toggleAllBtn.classList.add('active');
  else toggleAllBtn.classList.remove('active');
  loading.style.display = 'block';
  loading.innerHTML = '<div class="spinner"></div>Reloading…';
  loadGraph();
});

document.getElementById('close-btn').addEventListener('click', () => {
  sidebar.classList.remove('open');
});

let storageMap = {};  // hub_id → items[]
let allNodes = null;
let allEdges = null;
let nodesDs = null;
let edgesDs = null;
let network = null;
// ★2026-07-05 記憶層オントロジー Phase 0: 配色モード (group=分類 / layer=記憶層)。
//   server の n.color (分類) は不変、n.color_layer (記憶層) との選択は _displayColor が担う
//   (= mutation なし、reload 時の再適用・snapshot の順序依存も消える、review altitude-5)。
const colorModeSel = document.getElementById('colormode');
// ブラウザの form 状態復元 (F5/bfcache) で select が復元されても JS 側が 'group' 固定だと
// UI と実配色が乖離し change も発火せず復旧不能 (review A-1/B-2/E-1) → select から初期化
let COLOR_MODE = (colorModeSel.value === 'layer') ? 'layer' : 'group';
let LAYER_META = [];
const legendEl = document.getElementById('legend');
const LEGEND_GROUP_HTML = legendEl.innerHTML;
const STORAGE_LEGEND_ROW =
  '<div class="row"><span class="sw diamond" style="background:#241d3a;border:1px solid #4a3f6b"></span><span class="label"> 思考のストレージ</span></div>';

function _displayColor(n) {
  return (COLOR_MODE === 'layer' && n.color_layer) ? n.color_layer : n.color;
}
function _glowShadow(n, col) {
  const glow = (col && col.background) || '#5A86A0';
  const sz = Math.max(5, Math.min(16, 5 + (n.value || 12) * 0.38));
  return { enabled: true, color: glow, size: sz, x: 0, y: 0 };
}

function renderLegend(mode) {
  if (mode !== 'layer' || !LAYER_META.length) {
    legendEl.innerHTML = LEGEND_GROUP_HTML;
    return;
  }
  legendEl.innerHTML = LAYER_META.map(l => {
    // bg は自 API 由来だが label と同じ境界で扱う: hex 形式のみ通す (review A-2/D-1)
    const bg = /^#[0-9a-fA-F]{3,8}$/.test(l.bg || '') ? l.bg : '#7E8597';
    return `<div class="row"><span class="sw" style="background:${bg};box-shadow:0 0 5px ${bg}99"></span>` +
      `<span class="label"> ${escapeHtml(l.label)}</span></div>`;
  }).join('') + STORAGE_LEGEND_ROW;
}

function applyColorMode(mode) {
  COLOR_MODE = (mode === 'layer') ? 'layer' : 'group';
  if (colorModeSel.value !== COLOR_MODE) colorModeSel.value = COLOR_MODE;
  renderLegend(COLOR_MODE);
  if (!nodesDs || !allNodes) return;
  const updates = [];
  allNodes.forEach(n => {   // storage hub も color_layer を持つ (縁色を凡例と整合、review B-1)
    const col = _displayColor(n);
    if (!col) return;
    updates.push({ id: n.id, color: col, shadow: _glowShadow(n, col) });
  });
  nodesDs.update(updates);
  _pulses = []; _bloom = {};   // 飛行中スパイクは発射時の旧配色を保持している → 一掃 (review A-4)
}
// bfcache 復帰などで select だけ復元された場合も同期
window.addEventListener('pageshow', () => {
  if (colorModeSel.value !== COLOR_MODE) applyColorMode(colorModeSel.value);
});
// ─────────────────────────────────────────────────────────────
// ★2026-06-20 もっと脳っぽく: スパイク伝播 (spreading activation)。
// ニューロンが発火 → シナプス(edge)を光のパルスが伝わり → 着信先が発火 →
// エネルギーを減衰させながら外へ波及。= 連想想起 (connectome) の可視化そのもの。
// afterDrawing で canvas に加算合成のブルームを重ね描き、rAF で駆動 (活動時のみ redraw)。
// ─────────────────────────────────────────────────────────────
let _adj = null;            // id -> [neighborId,...] (無向)
let _pos = null;            // id -> {x,y} (stabilize 後に cache)
let _pulses = [];           // 伝播中の光パルス
let _bloom = {};            // id -> 発光強度 (着信で増え、毎フレーム減衰)
let _rafId = null;
let _cascadeTimer = null;
let _lastDraw = 0;
const _PULSE_CAP = isMobile ? 40 : 140;

function _hex8(hex, a) {     // '#6E93A8' + alpha(0..1) -> '#6E93A8aa'
  const h = String(hex || '#7fb4ff').replace('#', '').slice(0, 6).padEnd(6, '0');
  const v = Math.max(0, Math.min(255, Math.round(a * 255))).toString(16).padStart(2, '0');
  return '#' + h + v;
}
function _nodeColor(id) {
  const n = allNodes && allNodes.find(x => x.id === id);
  const col = n && _displayColor(n);
  return (col && col.background) || '#7fb4ff';
}
function _buildAdjacency() {
  _adj = {};
  (allEdges || []).forEach(e => {
    if (!e.from || !e.to) return;
    if (String(e.from).startsWith('__storage__') || String(e.to).startsWith('__storage__')) return;
    (_adj[e.from] = _adj[e.from] || []).push(e.to);
    (_adj[e.to] = _adj[e.to] || []).push(e.from);
  });
}
function _emitPulse(from, to, color, energy, depth) {
  if (_pulses.length >= _PULSE_CAP) return;
  _pulses.push({ from, to, t: 0, speed: 0.010 + Math.random() * 0.012,
    color, size: 3 + energy * 4, energy, depth });
}
function _fireNeuron(id, energy, depth) {
  _bloom[id] = Math.min(1.8, (_bloom[id] || 0) + energy);
  if (energy < 0.28 || depth > 5) return;          // 減衰しきったら波及を止める
  const nbrs = _adj[id] || [];
  if (!nbrs.length) return;
  const fan = Math.min(nbrs.length, isMobile ? 2 : 3); // fanout を絞り爆発を防ぐ
  const picked = new Set();
  for (let k = 0; k < fan * 3 && picked.size < fan; k++) picked.add(nbrs[(Math.random() * nbrs.length) | 0]);
  picked.forEach(nb => _emitPulse(id, nb, _nodeColor(id), energy * 0.62, depth + 1));
}
function _seedCascade() {
  if (document.hidden || !_adj) return;
  if (_focusId) return;                            // focus 中は ambient を止め注意を集中
  if (_pulses.length > _PULSE_CAP * 0.5) return;   // 混雑時は新規 seed しない
  const ids = Object.keys(_adj);
  if (ids.length) _fireNeuron(ids[(Math.random() * ids.length) | 0], 1.6, 0);
}

// ─────────────────────────────────────────────────────────────
// ★2026-07-12 海山「関連の繋がりが動的に見れる様に」: Focus mode。
// ノード選択 → 全体を veil で沈め、そのノードの結合 (シナプス) だけを発光ラインで
// 浮かび上がらせ、信号パルスを結合に沿って流し続ける。空白タップ / Esc で解除。
// desktop hover は veil 無しの軽い結合ハイライトのみ (Apple 的な抑制)。
// ─────────────────────────────────────────────────────────────
let _focusId = null;
let _focusEdges = [];
let _focusTimer = null;
let _hoverId = null;

function _edgesOf(id) {
  return (allEdges || []).filter(e =>
    (e.from === id || e.to === id) &&
    !String(e.from).startsWith('__storage__') && !String(e.to).startsWith('__storage__'));
}
function setFocus(id) {
  if (_focusTimer) { clearInterval(_focusTimer); _focusTimer = null; }
  _focusId = id || null;
  _focusEdges = _focusId ? _edgesOf(_focusId) : [];
  if (_focusId && _focusEdges.length) {
    let i = 0;
    const links = _focusEdges.filter(e => e.type !== 'tag');
    const list = links.length ? links : _focusEdges;
    const burst = () => {
      if (!_focusId || document.hidden) return;
      for (let k = 0; k < Math.min(3, list.length); k++) {
        const e = list[(i++) % list.length];
        const out = e.from === _focusId;
        // depth=5 → 着信先で bloom するが波及はしない (focus の外に伝播させない)
        _emitPulse(out ? e.from : e.to, out ? e.to : e.from, _nodeColor(_focusId), 0.9, 5);
      }
      if (network) network.redraw();
    };
    burst();
    _focusTimer = setInterval(burst, 620);
  }
  if (network) network.redraw();
}
function _drawFocus(ctx) {
  const fid = _focusId || _hoverId;
  if (!fid || !_pos || !network) return;
  const edges = (fid === _focusId) ? _focusEdges : _edgesOf(fid);
  if (_focusId) {
    // 全体を沈める veil (screen 座標で全面塗り → world 座標に戻して focus 層を上描き)
    const cv = network.canvas.frame.canvas;
    ctx.save();
    ctx.setTransform(1, 0, 0, 1, 0, 0);
    ctx.fillStyle = 'rgba(8,7,11,0.74)';
    ctx.fillRect(0, 0, cv.width, cv.height);
    ctx.restore();
  }
  ctx.save();
  ctx.globalCompositeOperation = 'lighter';
  ctx.lineCap = 'round';
  // 結合 (シナプス) を発光ラインで
  for (const e of edges) {
    const a = _pos[e.from], b = _pos[e.to];
    if (!a || !b) continue;
    const col = _nodeColor(e.from === fid ? e.to : e.from);
    const isTag = e.type === 'tag';
    ctx.strokeStyle = _hex8(col, _focusId ? (isTag ? 0.14 : 0.46) : (isTag ? 0.08 : 0.28));
    ctx.lineWidth = isTag ? 0.7 : (e.type === 'bridge' ? 2.2 : 1.5);
    ctx.beginPath(); ctx.moveTo(a.x, a.y); ctx.lineTo(b.x, b.y); ctx.stroke();
  }
  // focus ノード + 隣接ニューロンを発光
  const seen = new Set([fid]);
  edges.forEach(e => { seen.add(e.from); seen.add(e.to); });
  for (const id of seen) {
    const p = _pos[id]; if (!p) continue;
    const col = _nodeColor(id), isF = id === fid;
    const r = isF ? 26 : 12;
    const g = ctx.createRadialGradient(p.x, p.y, 0, p.x, p.y, r);
    g.addColorStop(0, _hex8(col, isF ? 0.8 : 0.5));
    g.addColorStop(0.5, _hex8(col, isF ? 0.32 : 0.16));
    g.addColorStop(1, _hex8(col, 0));
    ctx.fillStyle = g; ctx.beginPath(); ctx.arc(p.x, p.y, r, 0, 6.2832); ctx.fill();
    ctx.fillStyle = _hex8(col, 0.95);
    ctx.beginPath(); ctx.arc(p.x, p.y, isF ? 6.5 : 3.8, 0, 6.2832); ctx.fill();
  }
  // ラベル (focus 中のみ、隣接 ≤22 個まで — screen 一定サイズ)
  if (_focusId && seen.size <= 23) {
    ctx.globalCompositeOperation = 'source-over';
    const sc = Math.max(0.35, network.getScale());
    ctx.font = (11.5 / sc) + 'px -apple-system, BlinkMacSystemFont, sans-serif';
    ctx.textAlign = 'center';
    for (const id of seen) {
      const p = _pos[id]; if (!p) continue;
      const n = allNodes && allNodes.find(x => x.id === id);
      const label = (n && (n.label || n.title)) || '';
      if (!label) continue;
      const y = p.y + (id === fid ? 22 : 14) / sc;
      ctx.fillStyle = 'rgba(8,7,11,0.75)';
      ctx.strokeStyle = 'rgba(8,7,11,0.75)'; ctx.lineWidth = 3.5 / sc;
      ctx.strokeText(label, p.x, y);
      ctx.fillStyle = id === fid ? '#f2f2f4' : '#c7c7cc';
      ctx.fillText(label, p.x, y);
    }
  }
  ctx.restore();
}
function _neuralFrame(ts) {
  _rafId = requestAnimationFrame(_neuralFrame);
  if (document.hidden || !network) return;
  if (!_pulses.length && !Object.keys(_bloom).length) return;  // idle: 描画しない(省電力)
  const keep = [];
  for (const p of _pulses) {
    p.t += p.speed;
    if (p.t >= 1) _fireNeuron(p.to, p.energy, p.depth);        // 着信 → 発火・波及
    else keep.push(p);
  }
  _pulses = keep;
  for (const id in _bloom) { _bloom[id] *= 0.9; if (_bloom[id] < 0.02) delete _bloom[id]; }
  const minDt = isMobile ? 38 : 16;                            // mobile は ~26fps に間引き
  if (!ts || ts - _lastDraw >= minDt) { _lastDraw = ts || 0; network.redraw(); }
}
function _drawNeural(ctx) {
  if (!_pos) return;
  _drawFocus(ctx);                                 // focus/hover 層を先に (パルスはその上を飛ぶ)
  ctx.save();
  ctx.globalCompositeOperation = 'lighter';        // 加算合成 = 発光ブルーム
  for (const p of _pulses) {                        // 伝播パルス(尾 + コア)
    const a = _pos[p.from], b = _pos[p.to];
    if (!a || !b) continue;
    const x = a.x + (b.x - a.x) * p.t, y = a.y + (b.y - a.y) * p.t;
    const tx = a.x + (b.x - a.x) * Math.max(0, p.t - 0.07), ty = a.y + (b.y - a.y) * Math.max(0, p.t - 0.07);
    ctx.strokeStyle = _hex8(p.color, 0.3); ctx.lineWidth = 1.3; ctx.lineCap = 'round';
    ctx.beginPath(); ctx.moveTo(tx, ty); ctx.lineTo(x, y); ctx.stroke();
    const r = p.size;
    const g = ctx.createRadialGradient(x, y, 0, x, y, r);
    g.addColorStop(0, _hex8(p.color, 0.58));
    g.addColorStop(0.45, _hex8(p.color, 0.2));
    g.addColorStop(1, _hex8(p.color, 0));
    ctx.fillStyle = g; ctx.beginPath(); ctx.arc(x, y, r, 0, 6.2832); ctx.fill();
  }
  for (const id in _bloom) {                         // 着信したニューロンの発光(鈍く)
    const pos = _pos[id]; if (!pos) continue;
    const inten = _bloom[id], r = 8 + inten * 16, col = _nodeColor(id);
    const g = ctx.createRadialGradient(pos.x, pos.y, 0, pos.x, pos.y, r);
    g.addColorStop(0, _hex8(col, Math.min(0.42, 0.28 * inten)));
    g.addColorStop(1, _hex8(col, 0));
    ctx.fillStyle = g; ctx.beginPath(); ctx.arc(pos.x, pos.y, r, 0, 6.2832); ctx.fill();
  }
  ctx.restore();
}
function startNeuralActivity() {
  if (_cascadeTimer) clearInterval(_cascadeTimer);
  if (_rafId) cancelAnimationFrame(_rafId);
  if (!network) return;
  _buildAdjacency();
  _pos = network.getPositions();                   // physics off 後の固定座標を cache
  _pulses = []; _bloom = {}; _lastDraw = 0;
  network.on('afterDrawing', _drawNeural);
  network.on('dragEnd', () => { _pos = network.getPositions(); }); // ドラッグ後に座標更新
  _cascadeTimer = setInterval(_seedCascade, isMobile ? 2200 : 1400);
  _seedCascade();
  _neuralFrame();
}

function loadGraph() {
  fetch(buildApiUrl())
    .then(r => {
      if (!r.ok) throw new Error('API ' + r.status + '. URL に ?key=<BRAIN_EXTENSION_KEY> または ?token=<VOICE_ALIGN_TOKEN> を付けてください');
      return r.json();
    })
    .then(data => renderGraph(data))
    .catch(e => {
      loading.innerHTML = '<div id="error">' + escapeHtml(e.message) + '</div>';
    });
}
loadGraph();

function renderGraph(data) {
  setFocus(null); _hoverId = null;   // reload 時に focus 層を初期化 (stale edge 参照を残さない)
  const s = data.stats;
  LAYER_META = data.layer_meta || [];
  renderLegend(COLOR_MODE);
  const layerLine = LAYER_META.length
    ? '<br>' + LAYER_META.map(l => `${escapeHtml(l.label.split(' ')[0])} ${l.count || 0}`).join(' · ')
    : '';
  statsEl.innerHTML =
    `<b>${s.surface_count}</b> 表示 / <b>${s.storage_count}</b> ストレージ<br>` +
    `total ${s.node_count} nodes, ${s.edge_count} edges<br>` +
    `surface ${s.surface_pct}%${s.show_all ? ' (all)' : ''}` + layerLine;

  // ストレージマップ作成
  storageMap = {};
  (data.storage || []).forEach(s => { storageMap[s.hub_id] = s.items; });

  allNodes = data.nodes;
  allEdges = data.edges;

  // モバイルでは tag エッジ非表示
  const visibleEdges = isMobile
    ? allEdges.filter(e => e.type !== 'tag')
    : allEdges;

  if (nodesDs) { nodesDs.clear(); edgesDs.clear(); }
  // ★2026-06-01 脳っぽく: 各ニューロン(node)に自身の色のグロー(shadow)を付与し、
  // 発火する細胞体のように暗い神経背景の上で光らせる。
  // ★2026-07-05: 初期 build から現在の配色モードで着色 (reload 後の二度塗りを排除)
  nodesDs = new vis.DataSet(allNodes.map(n => {
    const col = _displayColor(n);
    return Object.assign({}, n, { color: col, shadow: _glowShadow(n, col) });
  }));
  // ★ エッジ = 樹状突起 / 軸索。直線グレーから、ひんやり発光するシナプス結合へ。
  // link エッジは desktop のみ organic な曲線 (smooth continuous)、mobile は直線(軽量)。
  edgesDs = new vis.DataSet(visibleEdges.map((e, i) => {
    const isStorage = e.to_storage || e.from && e.from.startsWith('__storage__');
    const isBridge = e.type === 'bridge';   // ★2026-07-05 承認済み孤島接続 = 少し温かく太く
    return {
      id: 'e' + i,
      from: e.from, to: e.to,
      dashes: e.dashes || isStorage,
      label: (isBridge && e.label) || undefined,   // tag エッジの label は描かない (regression 防止)
      color: e.type === 'tag'
        ? { color: '#6a4fb0', opacity: 0.13 }
        : isBridge
          ? { color: '#9a7484', opacity: 0.42 }
          : isStorage
            ? { color: '#5a4a9a', opacity: 0.20 }
            : { color: '#6b7689', opacity: 0.22 },
      width: e.type === 'tag' ? 0.5 : (isBridge ? 1.6 : (isStorage ? 0.8 : 1.0)),
      font: (isBridge && e.label) ? { color: '#8d7580', size: 9, strokeWidth: 2, strokeColor: '#111111' } : undefined,
      smooth: (!isMobile && e.type !== 'tag' && !isStorage)
        ? { type: 'continuous', roundness: 0.35 }
        : false,
    };
  }));

  const container = document.getElementById('graph');

  const physics = isMobile ? {
    solver: 'barnesHut',
    barnesHut: {
      gravitationalConstant: -3500,
      centralGravity: 0.18,
      springLength: 100,
      springConstant: 0.045,
      damping: 0.4,
    },
    stabilization: { iterations: 100, updateInterval: 25 },
    timestep: 0.4,
  } : {
    solver: 'forceAtlas2Based',
    forceAtlas2Based: {
      gravitationalConstant: -55,
      centralGravity: 0.012,
      springLength: 130,
      springConstant: 0.08,
      damping: 0.5,
    },
    stabilization: { iterations: 250 },
  };

  if (network) network.destroy();
  network = new vis.Network(container, { nodes: nodesDs, edges: edgesDs }, {
    nodes: {
      shape: 'dot',
      borderWidth: 1.8,
      borderWidthSelected: 3,
      scaling: { min: 8, max: 60, label: { enabled: true, min: 11, max: 20 } },
      shadow: { enabled: false },
    },
    edges: { smooth: false, arrows: { to: { enabled: false } } },
    physics: physics,
    interaction: {
      hover: !isMobile,
      tooltipDelay: 300,
      multiselect: false,
      dragView: true,
      zoomView: true,
      zoomSpeed: 0.6,
      hideEdgesOnDrag: allNodes.length > 100,
    },
  });

  network.once('stabilizationIterationsDone', () => {
    loading.style.display = 'none';
    network.setOptions({ physics: false });
    startNeuralActivity();
  });
  setTimeout(() => { loading.style.display = 'none'; }, 6000);

  network.on('click', (params) => {
    if (params.nodes.length === 0) { setFocus(null); return; }  // 空白タップで focus 解除
    const id = params.nodes[0];
    if (id.startsWith('__storage__')) {
      setFocus(null);
      showStorage(id);
    } else {
      const n = allNodes.find(x => x.id === id);
      if (n) { setFocus(id); showDetail(n); }
    }
  });
  // desktop hover = veil 無しの軽い結合ハイライト
  if (!isMobile) {
    network.on('hoverNode', (p) => {
      if (String(p.node).startsWith('__storage__')) return;
      _hoverId = p.node; network.redraw();
    });
    network.on('blurNode', () => { _hoverId = null; network.redraw(); });
  }
}
document.addEventListener('keydown', (e) => {
  if (e.key === 'Escape') setFocus(null);
});

function showDetail(n) {
  const tags = (n.tags || []).map(t => `<span class="tag">${escapeHtml(t)}</span>`).join('');
  const title = n.title || n.label_full || n.label || n.id;
  const ageLabel = n.days_old < 7 ? `<b style="color:#6E93A8">今週</b>` :
                   n.days_old < 30 ? `<b style="color:#5E987F">${n.days_old}日前</b>` :
                   n.days_old < 90 ? `${n.days_old}日前` :
                   `<span style="color:#666">${n.days_old}日前</span>`;
  detail.innerHTML = `
    <h2>${escapeHtml(title)}</h2>
    <div class="meta">
      ${escapeHtml(n.path)}<br>
      ${ageLabel} · ${n.size.toLocaleString()}字 · score <b>${n.score}</b> · 被参照 ${n.in_degree}
    </div>
    <div>${tags}</div>
    <div style="margin-top:10px; color:#888"><em>読み込み中...</em></div>
  `;
  sidebar.classList.add('open');
  if (isMobile) sidebar.scrollTop = 0;
  fetch(wikiApi(n.path))
    .then(r => {
      // ★2026-07-11: r.ok を見ずに body を本文描画すると 404 の {"detail":"Not found"} が
      //   そのまま「本文」として出る (可視性ゲート時の混乱源) → status を明示的に判定。
      if (!r.ok) throw new Error('status ' + r.status);
      return r.text();
    })
    .then(text => {
      detail.innerHTML = `
        <h2>${escapeHtml(title)}</h2>
        <div class="meta">
          ${escapeHtml(n.path)}<br>
          ${ageLabel} · ${n.size.toLocaleString()}字 · score <b>${n.score}</b> · 被参照 ${n.in_degree}
        </div>
        <div>${tags}</div>
        <pre>${escapeHtml(text)}</pre>
      `;
    })
    .catch(() => {
      detail.innerHTML += '<div style="color:#f66">本文を取得できませんでした（可視性ゲート、または ?key= が admin 鍵でない）</div>';
    });
}

function showStorage(hubId) {
  const items = storageMap[hubId] || [];
  const group = hubId.replace('__storage__', '');
  detail.innerHTML = `
    <h2>💭 ${escapeHtml(group)} のストレージ</h2>
    <div class="meta">${items.length} 件 — score 順</div>
    <div class="storage-list">
      ${items.map(it => `
        <div class="storage-item" data-path="${escapeHtml(it.path)}" data-id="${escapeHtml(it.id)}">
          <span class="title">${escapeHtml(it.title)}</span>
          <span class="badge">s ${it.score}</span>
          <span class="age">${it.days_old}日</span>
        </div>
      `).join('')}
    </div>
  `;
  sidebar.classList.add('open');
  // クリックハンドラ
  detail.querySelectorAll('.storage-item').forEach(el => {
    el.addEventListener('click', () => {
      const path = el.dataset.path;
      // ダミーのn objectを作って showDetail に渡す
      const item = items.find(it => it.id === el.dataset.id);
      if (!item) return;
      const fakeNode = {
        title: item.title, path: item.path, size: item.size,
        days_old: item.days_old, score: item.score, in_degree: item.in_degree,
        tags: [], confidence: '', updated: '',
      };
      // back ボタン付きでdetail更新
      showDetailWithBack(fakeNode, hubId);
    });
  });
}

function showDetailWithBack(n, hubId) {
  showDetail(n);
  // 先頭に back を挟む
  setTimeout(() => {
    const back = document.createElement('div');
    back.className = 'back-btn';
    back.innerHTML = '← ストレージ一覧に戻る';
    back.addEventListener('click', () => showStorage(hubId));
    detail.insertBefore(back, detail.firstChild);
  }, 0);
}

let searchTimer;
document.getElementById('search').addEventListener('input', (e) => {
  clearTimeout(searchTimer);
  searchTimer = setTimeout(() => applyFilter(
    e.target.value.toLowerCase(),
    document.getElementById('filter').value
  ), 200);
});
document.getElementById('filter').addEventListener('change', (e) => {
  applyFilter(document.getElementById('search').value.toLowerCase(), e.target.value);
});
document.getElementById('colormode').addEventListener('change', (e) => {
  applyColorMode(e.target.value);
});
// ★2026-07-12 Apple segmented control ↔ hidden select 同期
const segEl = document.getElementById('colormode-seg');
function syncSeg() {
  segEl.querySelectorAll('button').forEach(b =>
    b.classList.toggle('on', b.dataset.v === COLOR_MODE));
}
segEl.querySelectorAll('button').forEach(b => {
  b.addEventListener('click', () => {
    colorModeSel.value = b.dataset.v;
    applyColorMode(b.dataset.v);
    syncSeg();
  });
});
window.addEventListener('pageshow', syncSeg);
syncSeg();

function applyFilter(q, group) {
  if (!nodesDs || !allNodes) return;
  setFocus(null);   // 表示集合が変わるので focus は解除 (隠れたノードへの veil 迷子を防ぐ)
  const matchNode = (n) => {
    if (group && n.group !== group && !(n.is_storage && n.storage_group === group)) return false;
    if (!q) return true;
    const label = (n.label_full || n.label || n.title || '').toLowerCase();
    return label.includes(q) ||
      (n.path || '').toLowerCase().includes(q) ||
      (n.tags || []).some(t => t.toLowerCase().includes(q));
  };
  const ids = new Set(allNodes.filter(matchNode).map(n => n.id));
  nodesDs.update(allNodes.map(n => ({ id: n.id, hidden: !ids.has(n.id) })));
  edgesDs.get().forEach(e => {
    edgesDs.update({ id: e.id, hidden: !(ids.has(e.from) && ids.has(e.to)) });
  });
}

function escapeHtml(s) {
  return String(s == null ? '' : s).replace(/[&<>"']/g, c => ({
    '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;'
  }[c]));
}

document.body.addEventListener('touchmove', (e) => {
  if (e.target.closest('#sidebar') || e.target.closest('#toolbar')) return;
  e.preventDefault();
}, { passive: false });
</script>
</body>
</html>
"""
