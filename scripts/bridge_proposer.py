#!/usr/bin/env python3
"""
bridge_proposer.py — 孤島接続の提案生成 (★2026-07-05 Phase 1、ADR wiki-ontology-multilayer §3)

グラフの星型 topology の主因 = judgment/analysis/decisions の orphan 率 88-100% (被リンク 0)。
この孤島への「関係候補」を 2 つの実測シグナルから算出し、propose-only で queue に積む:
  ① 共起 (connectome): 実際の想起ログで「一緒に recall された」ペア = 経験的な連想
  ② embedding 類似: 本文の意味的近さ (LiteLLM text-embedding-3-small、mtime cache)

**絶対不変**: 本 script は wiki に一切書かない。採用は海山の /bridge 承認のみ
(reflux と同型)。捏造対策: 提案の根拠は数値 (共起回数/cosine) のみで LLM の作文なし、
relation は無型 "related" 固定 (型付けは compile 由来提案のみ、それも閉語彙に正規化)。
§1.17: 深層 private は走査・提案・queue 文面の全段で除外。

Usage:
  python3 scripts/bridge_proposer.py --dry-run          # 提案を表示のみ
  python3 scripts/bridge_proposer.py --push             # queue 追加 + LINE 通知
  cron: 20 2 * * * scripts/bridge_cron.sh (BRIDGE_PROPOSER_ENABLED=0 で opt-out)
"""
from __future__ import annotations

import argparse
import json
import logging
import math
import os
import re
import sys
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))        # scripts/ sibling
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))    # repo root

from connectome import build_cooccurrence_graph  # noqa: E402
from connectome_build import _iter_recall_events  # noqa: E402
from brain_wiki_helpers.domain import is_deep_private_rel  # noqa: E402
from brain_wiki_helpers.ontology import node_kind_of  # noqa: E402
from brain_wiki_helpers.edge_store import (  # noqa: E402
    append_proposals, existing_pair_keys, list_pending, pair_key,
)

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger("bridge")

ROOT = Path(__file__).resolve().parents[1]
BRAIN_ROOT = ROOT / "data" / "brain"
WIKI_DIR = BRAIN_ROOT / "wiki"
EVENTS = BRAIN_ROOT / "bot_events" / "events.jsonl"
EMBED_CACHE = BRAIN_ROOT / "graph" / "embed_cache.json"

ORPHAN_DIRS = ("judgment", "analysis", "decisions")   # 孤島の対象 (ADR §3)
WIKI_LINK_RE = re.compile(r"\[\[([^\]|]+)(?:\|[^\]]+)?\]\]")
EMBED_MODEL = os.getenv("BRIDGE_EMBED_MODEL", "text-embedding-3-small")
MIN_COS = float(os.getenv("BRIDGE_MIN_COS", "0.78"))
MIN_COOC = float(os.getenv("BRIDGE_MIN_COOC", "2.0"))
TOP_K_PER_ORPHAN = 2


# ─── wiki スキャン (chromadb 非接触、markdown 直読) ───

def _norm_key(s: str) -> str:
    s = s.strip().lower()
    return s[:-3] if s.endswith(".md") else s


def scan_wiki(wiki_dir: Path) -> dict:
    """files / key→rel 解決 map / 既存 wikilink pair / in_degree を 1 pass で。"""
    files: dict[str, str] = {}          # rel -> 本文先頭 (embed 入力)
    key_to_rel: dict[str, str] = {}
    for md in sorted(wiki_dir.rglob("*.md")):
        rel = str(md.relative_to(wiki_dir))
        parts = Path(rel).parts
        if any(p.startswith(".") for p in parts):
            continue
        if is_deep_private_rel(rel):                 # §1.17 walk 段階で除外
            continue
        try:
            text = md.read_text(encoding="utf-8", errors="replace")
        except Exception:
            continue
        files[rel] = text
        for k in (_norm_key(rel), _norm_key(Path(rel).stem)):
            key_to_rel.setdefault(k, rel)

    linked: set[tuple[str, str]] = set()
    in_degree: dict[str, int] = {r: 0 for r in files}
    for rel, text in files.items():
        # ★index (カタログ) からのリンクは in_degree に数えない: 本番 root index.md は
        #   1,540 links = 全ページにリンクしており (Karpathy 式 1行カタログ)、数えると
        #   孤島が 0 件になる (実測 2026-07-05)。カタログ掲載 ≠ 連想接続。
        #   linked (dedup 用) には入れる = index との bridge 提案は無意味なので抑止。
        src_is_index = node_kind_of(rel) == "index"
        for m in WIKI_LINK_RE.finditer(text):
            dst = key_to_rel.get(_norm_key(m.group(1)))
            if dst and dst != rel:
                linked.add(pair_key(rel, dst))
                if not src_is_index:
                    in_degree[dst] += 1
    return {"files": files, "linked": linked, "in_degree": in_degree}


def find_orphans(scan: dict) -> list[str]:
    return [
        rel for rel, deg in scan["in_degree"].items()
        if deg == 0 and Path(rel).parts and Path(rel).parts[0] in ORPHAN_DIRS
    ]


# ─── シグナル ① 共起 (実 recall ログ) ───

def _norm_recall_id(s: str) -> str:
    s = str(s or "").strip()
    return s[5:] if s.startswith("wiki/") else s


def cooc_proposals(scan: dict, orphans: list[str], events_path: Path) -> list[dict]:
    if not events_path.exists():
        return []
    events = [{"doc_ids": [_norm_recall_id(x) for x in recall], "quality": 1.0}
              for recall in _iter_recall_events(events_path)]
    if not events:
        return []
    g = build_cooccurrence_graph(events)
    orphan_set, files = set(orphans), scan["files"]
    out = []
    for a, nbrs in g.items():
        for b, w in nbrs.items():
            if w < MIN_COOC or a >= b:              # 対称グラフの片側だけ
                continue
            if a not in files or b not in files:
                continue
            if a not in orphan_set and b not in orphan_set:
                continue
            if pair_key(a, b) in scan["linked"]:
                continue
            out.append({"from": a, "to": b, "relation": "related", "source": "cooc",
                        "why": f"同時想起 {w:.0f}回", "score": float(w)})
    return out


# ─── シグナル ② embedding 類似 ───

def _embed_batch(texts: list[str]) -> list[list[float]]:
    url = os.getenv("LITELLM_URL", "http://localhost:4000").rstrip("/") + "/v1/embeddings"
    key = os.getenv("LITELLM_MASTER_KEY", "")
    body = json.dumps({"model": EMBED_MODEL, "input": texts}).encode()
    req = urllib.request.Request(url, data=body, method="POST", headers={
        "Authorization": f"Bearer {key}", "Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=120) as r:
        data = json.loads(r.read())
    items = sorted(data["data"], key=lambda d: d["index"])
    return [d["embedding"] for d in items]


def get_embeddings(scan: dict, wiki_dir: Path) -> dict[str, list[float]]:
    """(rel, mtime) キャッシュ付きで全 file の embedding を得る。"""
    cache: dict[str, dict] = {}
    if EMBED_CACHE.exists():
        try:
            cache = json.loads(EMBED_CACHE.read_text(encoding="utf-8"))
        except Exception:
            cache = {}
    vecs: dict[str, list[float]] = {}
    todo: list[str] = []
    mtimes: dict[str, float] = {}          # scan 時点の 1 回 stat を再利用 (再 stat の競合/例外を排除)
    for rel in scan["files"]:
        try:
            mtimes[rel] = (wiki_dir / rel).stat().st_mtime
        except OSError:
            continue
        c = cache.get(rel)
        if c and c.get("mtime") == mtimes[rel] and c.get("model") == EMBED_MODEL:
            vecs[rel] = c["vec"]
        else:
            todo.append(rel)
    for i in range(0, len(todo), 48):
        batch = todo[i:i + 48]
        texts = [scan["files"][r][:1600] for r in batch]
        try:
            embs = _embed_batch(texts)
        except Exception as e:
            logger.warning(f"embed batch 失敗 ({len(batch)}件 skip): {e}")
            continue
        for rel, v in zip(batch, embs):
            vecs[rel] = v
            cache[rel] = {"mtime": mtimes[rel], "model": EMBED_MODEL, "vec": v}
    # ★stale prune (code-review privacy-2): 削除/改名/deep-private 化された file の
    # embedding が cache に永続し restic offsite へ乗り続けるのを防ぐ — 現存 scan 集合に限定
    cache = {r: c for r, c in cache.items() if r in scan["files"]}
    EMBED_CACHE.parent.mkdir(parents=True, exist_ok=True)
    EMBED_CACHE.write_text(json.dumps(cache), encoding="utf-8")
    return vecs


def _cos(a: list[float], b: list[float]) -> float:
    dot = sum(x * y for x, y in zip(a, b))
    na = math.sqrt(sum(x * x for x in a)) or 1e-9
    nb = math.sqrt(sum(x * x for x in b)) or 1e-9
    return dot / (na * nb)


def embed_proposals(scan: dict, orphans: list[str], vecs: dict[str, list[float]]) -> list[dict]:
    out = []
    # partner から index (カタログ) を除外 — カタログとの「類似」は接続として無意味
    partners = [r for r in vecs
                if r not in set(orphans) and node_kind_of(r) != "index"]
    for o in orphans:
        ov = vecs.get(o)
        if not ov:
            continue
        sims = sorted(
            ((r, _cos(ov, vecs[r])) for r in partners
             if pair_key(o, r) not in scan["linked"]),
            key=lambda t: t[1], reverse=True,
        )[:TOP_K_PER_ORPHAN]
        for r, c in sims:
            if c < MIN_COS:
                continue
            out.append({"from": o, "to": r, "relation": "related", "source": "embed",
                        "why": f"embedding 類似 {c:.2f}", "score": round(c, 4)})
    return out


# ─── run / CLI / LINE ───

def run(*, dry_run: bool = False, max_n: int = 30, push: bool = False,
        events_path: Path = EVENTS) -> dict:
    scan = scan_wiki(WIKI_DIR)
    orphans = find_orphans(scan)
    logger.info(f"wiki {len(scan['files'])} files / 孤島 {len(orphans)} 件 ({','.join(ORPHAN_DIRS)})")
    cands = cooc_proposals(scan, orphans, events_path)
    vecs = get_embeddings(scan, WIKI_DIR) if orphans else {}
    cands += embed_proposals(scan, orphans, vecs)
    # pair 内 dedup (cooc 優先 = 実測 > 意味近似) → score 降順
    by_pair: dict[tuple, dict] = {}
    for c in cands:
        k = pair_key(c["from"], c["to"])
        if k not in by_pair or (c["source"] == "cooc" and by_pair[k]["source"] != "cooc"):
            by_pair[k] = c
    ranked = sorted(by_pair.values(), key=lambda c: (c["source"] != "cooc", -c["score"]))
    # ★バッチ多様性 (2026-07-05 初回実走: 巨大孤島 1 件が上位 9 枠を独占): 1 ノード最大 4 提案/run。
    #   残りは翌日以降の run が拾う (dedup は pair 単位なので取りこぼさない)。
    per_node: dict[str, int] = {}
    cands = []
    for c in ranked:
        if per_node.get(c["from"], 0) >= 4 or per_node.get(c["to"], 0) >= 4:
            continue
        per_node[c["from"]] = per_node.get(c["from"], 0) + 1
        per_node[c["to"]] = per_node.get(c["to"], 0) + 1
        cands.append(c)
        if len(cands) >= max_n:
            break
    if dry_run:
        for c in cands:
            print(f"  {c['from']} — {c['to']} ({c['why']})")
        return {"ok": True, "dry_run": True, "candidates": len(cands)}
    added = append_proposals(BRAIN_ROOT, WIKI_DIR, cands)
    logger.info(f"queue 追加 {added} 件 (候補 {len(cands)})")
    if push and added:
        try:
            from clone_improve_lib import line_push
            top = list_pending(BRAIN_ROOT)[:3]
            preview = "\n".join(
                f"・{_side_label(r['from'])} ↔ {_side_label(r['to'])}\n  ({_display_why(r)})"
                for r in top)
            line_push(f"🌉 Brain Map の「記憶の接続」提案が {added} 件届きました。例:\n{preview}\n"
                      f"→ /bridge で全一覧、/bridge ok all で一括承認 (承認まで graph 不変)")
        except Exception as e:
            logger.warning(f"LINE push 失敗: {e}")
    return {"ok": True, "added": added, "candidates": len(cands)}


# ─── 承認 UI の可読化 (★2026-07-05 海山 feedback「中身が分からない」) ───

_DIR_LABELS = {
    "judgment": "判断軸", "style": "文体", "knowledge": "知識", "analysis": "分析",
    "decisions": "決定", "hobbies": "趣向", "people": "人物", "projects": "PJ",
    "meetings": "会議", "sales": "売上",
}


def _clip_title(s: str, n: int = 34) -> str:
    """タイトルの切り詰め。尻切れの開き括弧/記号を残さない (「集約 (」の見苦しさ対策)。"""
    s = s.strip()
    if len(s) <= n:
        return s
    return re.sub(r"[\s(（\[「【<＜/—–\-:：・]+$", "", s[:n]) + "…"


def _title_of(rel: str) -> str:
    """wiki file の # 見出し (人間が読むタイトル)。無ければ file 名 stem。"""
    try:
        with (WIKI_DIR / rel).open(encoding="utf-8", errors="replace") as f:
            for _ in range(30):
                line = f.readline()
                if not line:
                    break
                if line.startswith("# "):
                    return _clip_title(line[2:])
    except OSError:
        pass
    return _clip_title(Path(rel).stem)


def _side_label(rel: str) -> str:
    d = Path(rel).parts[0] if len(Path(rel).parts) > 1 else ""
    lab = _DIR_LABELS.get(d, d or "wiki")
    return f"「{_title_of(rel)}」({lab})"


def _display_why(rec: dict) -> str:
    """根拠を平文で (queue の terse な why を人間語に再レンダリング)。"""
    src, score = rec.get("source"), rec.get("score", 0)
    if src == "cooc":
        return f"実際の会話で {score:.0f}回 一緒に想起された"
    if src == "embed":
        return f"書かれている内容が近い (類似度 {score:.2f})"
    if src == "compile":
        return "wiki 整理時に AI が関連と提案 (要確認)"
    return rec.get("why", "")


def render_pending(pend: list[dict], page: int = 1, per: int = 8) -> str:
    total = len(pend)
    pages = max(1, (total + per - 1) // per)
    page = max(1, min(page, pages))
    chunk = pend[(page - 1) * per: page * per]
    lines = [f"🌉 記憶の接続 提案 {total}件 (承認したものだけ Brain Map に繋がる、wiki は不変)"]
    if pages > 1:
        lines[0] += f" — {page}/{pages}頁"
    for i, r in enumerate(chunk, start=(page - 1) * per + 1):
        sid = r["id"][3:7]   # 承認は先頭4字で OK
        lines.append(f"{i}. [{sid}] {_side_label(r['from'])}")
        lines.append(f"   ↔ {_side_label(r['to'])}")
        lines.append(f"   根拠: {_display_why(r)}")
    if pages > 1 and page < pages:
        lines.append(f"次頁: /bridge {page + 1}")
    lines.append("承認: /bridge ok <4字id> (複数可) / 全部: /bridge ok all / 却下: /bridge ng <id> / 戻す: /bridge undo <id>")
    return "\n".join(lines)


def _resolve_ids(given: list[str], pend: list[dict]) -> tuple[list[str], list[str]]:
    """入力 id を pending に解決。完全一致 → 一意 prefix (br- 省略可、≥4字) の順。
    (LINE モバイルで br-xxxxxxxx 全打ちは pileup の元 = code-review UX-5)"""
    pool = [r["id"] for r in pend]
    ok, bad = [], []
    for g in given:
        g2 = g if g.startswith("br-") else f"br-{g}"
        if g in pool or g2 in pool:
            ok.append(g if g in pool else g2)
            continue
        stem = g[3:] if g.startswith("br-") else g
        if len(stem) >= 4:
            hits = [i for i in pool if i.startswith(f"br-{stem}")]
            if len(hits) == 1:
                ok.append(hits[0])
                continue
        bad.append(g)
    return ok, bad


def handle_command(arg: str) -> str:
    """LINE /bridge <arg> (admin gate は呼び出し元 main.py の fail-closed tuple)。

    '' 一覧 | ok all / ok <id|prefix>… | ng <id|prefix>… (ng all は all! で確認) | undo <id>…
    """
    from brain_wiki_helpers.edge_store import decide, load_approved_edges, reopen
    arg = (arg or "").strip()
    if not arg or arg.isdigit():
        pend = list_pending(BRAIN_ROOT)
        n_edges = len(load_approved_edges(BRAIN_ROOT))
        if not pend:
            return f"🌉 bridge: 承認待ちは無し (Brain Map の接続済みエッジ {n_edges} 本)"
        return render_pending(pend, page=int(arg) if arg.isdigit() else 1)
    parts = arg.split()
    op, ids = parts[0].lower(), parts[1:]
    if op not in ("ok", "ng", "no", "reject", "undo") or not ids:
        return "使い方: /bridge | /bridge ok all | /bridge ok <id>… | /bridge ng <id>… | /bridge undo <id>…"
    if "all" in ids or "all!" in ids:
        if len(ids) > 1:      # 'ok all br-x' の混在は silent drop せず明示エラー (review UX-4)
            return "⚠️ all と個別 id は同時指定できません。/bridge ok all か /bridge ok <id>… のどちらかで。"
        if op == "ok":
            r = decide(BRAIN_ROOT, ["all"], approve=True)
            return f"✅ 一括承認 → graph に反映: {len(r['done'])} 件"
        if op == "undo":
            return "undo は個別 id 指定のみ (誤操作防止)"
        # ng all は取り返しが重い (dedup により再提案されない) → 確認付き (review UX-2)
        if ids[0] != "all!":
            return "⚠️ 全却下は再提案されません (戻すのは /bridge undo)。本当に良ければ /bridge ng all!"
        r = decide(BRAIN_ROOT, ["all"], approve=False)
        return f"🗑 一括却下: {len(r['done'])} 件 (/bridge undo <id> で個別に戻せます)"
    pend = list_pending(BRAIN_ROOT)
    if op == "undo":
        r = reopen(BRAIN_ROOT, ids)
        msg = f"↩️ pending に戻しました: {len(r['done'])} 件"
        if r["skipped"]:
            msg += f" (対象外: {', '.join(r['skipped'][:5])})"
        return msg
    resolved, bad = _resolve_ids(ids, pend)
    r = decide(BRAIN_ROOT, resolved, approve=(op == "ok")) if resolved else {"done": [], "skipped": []}
    verb = "✅ 承認 → graph に反映" if op == "ok" else "🗑 却下"
    msg = f"{verb}: {len(r['done'])} 件"
    if bad or r["skipped"]:
        msg += f" (不明 id: {', '.join((bad + r['skipped'])[:5])})"
    return msg


def main() -> int:
    ap = argparse.ArgumentParser(description="孤島接続の提案生成 (propose-only)")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--max", type=int, default=30)
    ap.add_argument("--push", action="store_true", help="新規提案があれば LINE 通知")
    ap.add_argument("--events", default=str(EVENTS))
    a = ap.parse_args()
    r = run(dry_run=a.dry_run, max_n=a.max, push=a.push, events_path=Path(a.events))
    print(r)
    return 0 if r.get("ok") else 1


if __name__ == "__main__":
    raise SystemExit(main())
