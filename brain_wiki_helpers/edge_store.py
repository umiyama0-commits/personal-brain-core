"""
brain_wiki_helpers/edge_store.py — 型付きエッジの sidecar store (★2026-07-05 Phase 1)

wiki の「関係」を frontmatter でなく sidecar (data/brain/graph/*.jsonl) に持つ。
理由 (ADR 2026-07-05 §4 で不採用を拘束): frontmatter 持続化は機械 writer 4 系統
(compile action=replace / judgment_extractor 週次 rewrite / merge_frontmatters の
quoting 破壊→scrub bypass) に轢かれることを code-review で実証済み。

- bridge_queue.jsonl: 提案 (pending/approved/rejected)。書くのは bridge_proposer (host cron)
  と compile 配線。決めるのは海山の /bridge のみ (propose-only 絶対)。
- edges.jsonl: 承認済みエッジ (append-only)。brain_graph が描画に読む。

data/brain/* は gitignore 済 = auto_deploy stash と衝突しない (§1.14)。
container とは ./data mount で共有。排他は reflux と同じ flock —
★制約 (code-review 2026-07-05): flock は host↔container の Docker bind mount を跨いで
効かない (別 kernel)。実害は append-only + id 後勝ち + dedup 自己修復で有界
(最悪 = 重複提案 or stale view への決定、どちらも次サイクルで収束)。同一側の writer 同士は正しく直列化。
★dedup は pair 単位・status 不問 (rejected 含む) = 1 pair 1 判断の noise 制御。却下の取り消しは
/bridge undo。型付き上書き (related → evidence_for) は意図的に不可 (欲しければ undo → 再提案)。
★restic (ADR §5-7): data/brain/graph/ は backup_offsite.sh の `restic backup data/brain` に乗る
(暗号化 offsite)。queue/edges は wiki の path 名のみ・deep-private は入構不可、embed_cache は
OWNDAYS-facing 本文の embedding のみ + 毎 run で現存 file に prune — wiki 本体と同じ信頼域と判断。
"""
from __future__ import annotations

import contextlib
import fcntl
import hashlib
import json
import os
from datetime import datetime
from pathlib import Path

from brain_wiki_helpers.domain import is_deep_private_rel
from brain_wiki_helpers.ontology import normalize_relation


def _graph_dir(brain_root: Path) -> Path:
    return Path(brain_root) / "graph"


def queue_path(brain_root: Path) -> Path:
    return _graph_dir(brain_root) / "bridge_queue.jsonl"


def edges_path(brain_root: Path) -> Path:
    return _graph_dir(brain_root) / "edges.jsonl"


@contextlib.contextmanager
def _lock(brain_root: Path):
    """queue/edges の read-modify-write 排他 (reflux._queue_lock と同流儀)。"""
    gd = _graph_dir(brain_root)
    gd.mkdir(parents=True, exist_ok=True)
    fd = os.open(str(gd / ".lock"), os.O_CREAT | os.O_RDWR, 0o644)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
        yield
    finally:
        try:
            fcntl.flock(fd, fcntl.LOCK_UN)
        finally:
            os.close(fd)


def _read_jsonl(path: Path) -> list[dict]:
    if not path.exists():
        return []
    out = []
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            out.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    return out


def _append_jsonl(path: Path, rec: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as f:
        f.write(json.dumps(rec, ensure_ascii=False) + "\n")


def pair_key(a: str, b: str) -> tuple[str, str]:
    return tuple(sorted((str(a), str(b))))  # type: ignore[return-value]


def proposal_id(a: str, b: str, relation: str) -> str:
    k = pair_key(a, b)
    h = hashlib.sha1(f"{k[0]}|{k[1]}|{relation}".encode()).hexdigest()[:8]
    return f"br-{h}"


def canonical_rel(rel: str, wiki_dir: Path) -> str | None:
    """rel を wiki_dir 封じ込めで正規化。wiki 外 (`..`/絶対 path) や非実在は None。

    ★code-review 2026-07-05 P1 (privacy-1): compile の new_connections は LLM 出力
    (外部由来 raw 文書 = injection 面)。'../wiki/personal/x.md' が先頭一致 check を
    すり抜けるため、domain.is_personal_path と同じ resolve+relative_to 二重チェックで封じる。
    """
    rel = str(rel or "").strip()
    if not rel or rel.startswith(("/", "~")):
        return None
    try:
        rp = (Path(wiki_dir) / rel).resolve()
        norm = rp.relative_to(Path(wiki_dir).resolve())
    except (ValueError, OSError):
        return None
    if not rp.is_file():
        return None
    return str(norm)


def validate_pair(frm: str, to: str, wiki_dir: Path) -> str | None:
    """エラー理由 or None (OK)。両端実在 (封じ込め済) + 深層 private 不可 + 自己ループ不可。

    ★§1.17: 深層 private を指すエッジは queue にすら入れない (提案文面から path 名が
    漏れるのも防ぐ)。実在チェックは dangling 提案 (compile の幻覚 path) を落とす。
    """
    cf, ct = canonical_rel(frm, wiki_dir), canonical_rel(to, wiki_dir)
    if cf is None or ct is None:
        return "wiki 外 or 実在しない endpoint"
    if cf == ct:
        return "自己ループ"
    for rel in (cf, ct):
        if is_deep_private_rel(rel):
            return "deep-private endpoint"
    return None


def existing_pair_keys(brain_root: Path) -> set[tuple[str, str]]:
    """queue (全 status) + edges 済の pair key 集合 (dedup 用)。"""
    keys = set()
    for rec in _read_jsonl(queue_path(brain_root)) + _read_jsonl(edges_path(brain_root)):
        if rec.get("from") and rec.get("to"):
            keys.add(pair_key(rec["from"], rec["to"]))
    return keys


def append_proposals(brain_root: Path, wiki_dir: Path, proposals: list[dict]) -> int:
    """提案を pending で queue に積む。dedup + validate 済のものだけ。返り値 = 追加数。

    proposal: {from, to, relation?, source, why?, score?}
    """
    added = 0
    with _lock(brain_root):
        seen = existing_pair_keys(brain_root)
        for p in proposals:
            if validate_pair(p.get("from", ""), p.get("to", ""), wiki_dir) is not None:
                continue
            # 保存は正規化 rel (traversal 文字列を queue に残さない)
            frm = canonical_rel(p.get("from", ""), wiki_dir)
            to = canonical_rel(p.get("to", ""), wiki_dir)
            # ★2026-07-10 (CI typecheck fix): canonical_rel は wiki 外/非実在で None を返す。
            #   validate_pair 済でも防御的に None を弾く (pyright str 保証 + traversal 二重防御)。
            if frm is None or to is None:
                continue
            if pair_key(frm, to) in seen:
                continue
            relation = normalize_relation(p.get("relation") or "")
            rec = {
                "id": proposal_id(frm, to, relation),
                "ts": datetime.now().isoformat(timespec="seconds"),
                "from": frm, "to": to, "relation": relation,
                "source": str(p.get("source", "unknown"))[:16],
                "why": str(p.get("why", ""))[:120],   # 根拠は数値要約のみ、本文は載せない (§1.17)
                "score": round(float(p.get("score", 0.0)), 4),
                "status": "pending",
            }
            _append_jsonl(queue_path(brain_root), rec)
            seen.add(pair_key(frm, to))
            added += 1
    return added


def list_pending(brain_root: Path) -> list[dict]:
    """pending 提案 (後勝ち: 同 id は最後の status が真)。score 降順。"""
    latest: dict[str, dict] = {}
    for rec in _read_jsonl(queue_path(brain_root)):
        if rec.get("id"):
            latest[rec["id"]] = rec
    pend = [r for r in latest.values() if r.get("status") == "pending"]
    return sorted(pend, key=lambda r: r.get("score", 0), reverse=True)


def decide(brain_root: Path, ids: list[str], approve: bool) -> dict:
    """承認/却下 (バッチ)。ids=['all'] で pending 全件。承認は edges.jsonl へ append。

    queue は append-only の決定レコード追記 (status 上書きでなく履歴が残る)。
    返り値: {ok, done: [id...], skipped: [id...]}
    """
    now = datetime.now().isoformat(timespec="seconds")
    done, skipped = [], []
    with _lock(brain_root):
        pend = {r["id"]: r for r in list_pending(brain_root)}
        targets = list(pend.values()) if ids == ["all"] else [
            pend[i] for i in ids if i in pend
        ]
        skipped = [i for i in ids if i != "all" and i not in pend]
        for r in targets:
            new_status = "approved" if approve else "rejected"
            _append_jsonl(queue_path(brain_root), {**r, "status": new_status, "decided_at": now})
            if approve:
                _append_jsonl(edges_path(brain_root), {
                    "id": r["id"], "from": r["from"], "to": r["to"],
                    "relation": r["relation"], "source": r["source"],
                    "approved_at": now,
                })
            done.append(r["id"])
    return {"ok": True, "done": done, "skipped": skipped}


def reopen(brain_root: Path, ids: list[str]) -> dict:
    """誤って却下/承認した提案を pending に戻す (undo)。edges.jsonl からは消さない
    (承認済エッジの削除は描画にのみ効く別問題 — pending に戻して ng し直せば
    load_approved_edges の初回優先で残るため、エッジ削除が要る時は手動編集)。
    """
    now = datetime.now().isoformat(timespec="seconds")
    done, skipped = [], []
    with _lock(brain_root):
        latest: dict[str, dict] = {}
        for rec in _read_jsonl(queue_path(brain_root)):
            if rec.get("id"):
                latest[rec["id"]] = rec
        for i in ids:
            r = latest.get(i)
            if not r or r.get("status") == "pending":
                skipped.append(i)
                continue
            _append_jsonl(queue_path(brain_root), {**r, "status": "pending", "reopened_at": now})
            done.append(i)
    return {"ok": True, "done": done, "skipped": skipped}


def load_approved_edges(brain_root: Path) -> list[dict]:
    """承認済みエッジ (描画用)。同 pair の重複は最初の 1 本。"""
    out, seen = [], set()
    for rec in _read_jsonl(edges_path(brain_root)):
        if not (rec.get("from") and rec.get("to")):
            continue
        k = pair_key(rec["from"], rec["to"])
        if k in seen:
            continue
        seen.add(k)
        out.append(rec)
    return out
