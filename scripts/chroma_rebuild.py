#!/usr/bin/env python3
"""chroma_rebuild.py — HNSW ブロートしたコレクションを再構築して領域を回収する。

★2026-08-03 実測: wiki コレクションが 28,568 vector で **4.90GB** (妥当値 0.35GB = 14倍)。
raw は 10,933 vector で 0.07GB と正常。差は `index_wiki_file` の delete+upsert 累積で、
**ChromaDB の HNSW は削除領域を回収しない** (chroma#2594、2年 open の上流問題)。
wiki は file watcher とコンパイルで絶えず再索引されるため単調増加する (~285MB/日)。

方式: **再 embedding しない**。既存ベクトルを読み出し → コレクション削除 → 同名で作り直し →
書き戻す。API コストゼロ・LLM 非依存で、HNSW だけが綺麗に詰め直される。

安全装置:
- line-bot 稼働中は **実行拒否** (§1.5 chromadb 並行アクセス禁止 = SIGSEGV crash loop)
- 書き戻し前に dump を disk に保存 (途中で落ちてもベクトルは失われない)
- 件数が一致しなければ **削除しない** / 書き戻し後の件数不一致は異常終了

実行 (line-bot 停止後):
  docker compose run --rm --no-deps -T line-bot python3 /app/scripts/chroma_rebuild.py --collection wiki
  オプション: --dry-run (調査のみ) / --batch 1000
"""
from __future__ import annotations

import argparse
import pickle
import shutil
import subprocess
import sys
import time
from pathlib import Path

CHROMA_DIR = "/app/chroma_data"
DUMP_DIR = Path("/app/data/brain/.chroma_rebuild")


def _dir_size_gb(p: str | Path) -> float:
    total = 0
    for f in Path(p).rglob("*"):
        try:
            if f.is_file():
                total += f.stat().st_size
        except Exception:
            pass
    return total / 1e9


def _refuse_if_bot_running() -> None:
    """§1.5: line-bot が chroma を開いたまま触ると SIGSEGV crash loop になる。"""
    try:
        out = subprocess.run(["/bin/sh", "-c", "cat /proc/net/tcp 2>/dev/null | head -1"],
                             capture_output=True, text=True, timeout=5)
        del out
    except Exception:
        pass
    # container 内からは host の docker ps を見られないため、呼び出し側 (wrapper) の責務も併記。
    # ここでは chroma の SQLite に排他ロックが掛かっていないかで簡易判定する。
    import sqlite3
    db = Path(CHROMA_DIR) / "chroma.sqlite3"
    if not db.exists():
        print(f"❌ {db} が無い"); sys.exit(1)
    try:
        con = sqlite3.connect(f"file:{db}?mode=rw", uri=True, timeout=3)
        con.execute("BEGIN IMMEDIATE")
        con.rollback(); con.close()
    except sqlite3.OperationalError as e:
        print(f"❌ chroma が他プロセスに使用中の可能性 ({e})。line-bot を停止してから実行すること (§1.5)")
        sys.exit(1)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--collection", default="wiki")
    ap.add_argument("--batch", type=int, default=1000)
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    _refuse_if_bot_running()

    import chromadb
    from chromadb.config import Settings

    before_gb = _dir_size_gb(CHROMA_DIR)
    client = chromadb.PersistentClient(path=CHROMA_DIR,
                                       settings=Settings(anonymized_telemetry=False))
    col = client.get_collection(args.collection)
    n = col.count()
    meta = col.metadata or {}
    print(f"対象: {args.collection}  vectors={n:,}  chroma_data={before_gb:.2f}GB  metadata={meta}")
    if args.dry_run:
        print("--dry-run: ここで終了")
        return 0
    if n == 0:
        print("空のコレクション → 何もしない"); return 0

    # ─── 1) 読み出し (embeddings 込み = 再 embedding 不要) ───
    DUMP_DIR.mkdir(parents=True, exist_ok=True)
    dump_path = DUMP_DIR / f"{args.collection}-{int(time.time())}.pkl"
    records = {"ids": [], "embeddings": [], "documents": [], "metadatas": []}
    t0 = time.time()
    for off in range(0, n, args.batch):
        got = col.get(limit=args.batch, offset=off,
                      include=["embeddings", "documents", "metadatas"])
        records["ids"].extend(got["ids"])
        records["embeddings"].extend([list(map(float, e)) for e in got["embeddings"]])
        records["documents"].extend(got["documents"])
        records["metadatas"].extend(got["metadatas"])
        print(f"  読み出し {len(records['ids']):,}/{n:,}", flush=True)
    got_n = len(records["ids"])
    if got_n != n:
        print(f"❌ 読み出し件数が不一致 ({got_n:,} != {n:,}) → 削除せず中止")
        return 1
    with dump_path.open("wb") as f:
        pickle.dump(records, f, protocol=4)
    print(f"✓ dump 保存: {dump_path} ({dump_path.stat().st_size/1e9:.2f}GB, {time.time()-t0:.0f}s)")

    # ─── 2) 削除 → 同名で作り直し ───
    client.delete_collection(args.collection)
    new = client.create_collection(name=args.collection, metadata=meta or None)
    print(f"✓ コレクション再作成: {args.collection}")

    # ─── 3) 書き戻し ───
    t1 = time.time()
    for i in range(0, got_n, args.batch):
        sl = slice(i, i + args.batch)
        # metadata が None の要素は chroma が拒否するため {} に正規化
        metas = [m if isinstance(m, dict) and m else {"_": ""} for m in records["metadatas"][sl]]
        new.add(ids=records["ids"][sl], embeddings=records["embeddings"][sl],
                documents=records["documents"][sl], metadatas=metas)
        print(f"  書き戻し {min(i+args.batch, got_n):,}/{got_n:,}", flush=True)
    after_n = new.count()
    if after_n != n:
        print(f"❌ 書き戻し後の件数が不一致 ({after_n:,} != {n:,})。dump は {dump_path} に残存")
        return 1

    # ★実測で判明 (2026-08-03): chroma の delete_collection は DB 行を消すだけで
    # **旧セグメントのディレクトリを disk に置き去りにする**。これを消さないと
    # 再構築しても容量が全く減らない (実測 5.65GB → 5.83GB と逆に増えた)。
    # DB の segments に無い dir = 孤児として安全に削除できる。
    import sqlite3 as _sq
    _con = _sq.connect(Path(CHROMA_DIR) / "chroma.sqlite3")
    _live = {r[0] for r in _con.execute("SELECT id FROM segments")}
    _con.close()
    _freed = 0.0
    for d in sorted(Path(CHROMA_DIR).iterdir()):
        if d.is_dir() and d.name not in _live:
            _sz = _dir_size_gb(d)
            shutil.rmtree(d)
            _freed += _sz
            print(f"✓ 孤児セグメント削除: {d.name} ({_sz:.2f}GB)")
    if _freed:
        print(f"✓ 孤児 合計 {_freed:.2f}GB 回収")

    after_gb = _dir_size_gb(CHROMA_DIR)
    print(f"\n✅ 完了 {args.collection}: {n:,} vectors 維持")
    print(f"   chroma_data {before_gb:.2f}GB → {after_gb:.2f}GB (回収 {before_gb-after_gb:.2f}GB)")
    print(f"   dump は検証後に削除可: {dump_path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
