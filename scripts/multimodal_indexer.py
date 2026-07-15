"""
multimodal_indexer.py — Multi-modal RAG Phase 1 (image embedding skeleton)

設計:
  既存システムは text のみ embedding (PDF/画像 → OCR → text → Chroma)。
  ★ 「画像そのものの内容を embedding 索引」が手付かず。

  例: 「会議室の白板に何書いてあった?」「あの画像と似た図を持ってる?」 型は
  text embedding じゃ取れない。

Phase 1 (このファイル):
  - skeleton + meta registry のみ
  - 実 CLIP / 音声 embedding は soft import (依存無くても crash しない)
  - `data/brain/multimodal/` に index metadata 保存 (.json)
  - 画像 1 枚 = 1 record (path / sha256 / caption / clip_embedding (option))
  - 検索は cosine similarity (numpy あれば、無ければ text caption substring)
  - 既存 Chroma collection は触らない (multimodal は別 store として隔離)

Phase 2 で実装:
  - CLIP-ViT-Base モデル DL + 推論パイプライン
  - 画像 → 512 次元 embedding → Chroma 統合 (collection="multimodal")
  - 音声 (Whisper feature) も同 collection に
  - Query: 「あの会議の白板」 → text → CLIP text encoder → 画像 vector で search

依存 (Phase 2):
  - sentence-transformers or transformers (CLIP weights)
  - Pillow / opencv-python
  - Apple Silicon なら MLX 使うのが速い (sentence-transformers の M2 適化版)

実行:
  python3 scripts/multimodal_indexer.py --add path/to/image.jpg --caption "VMV 会議の白板"
  python3 scripts/multimodal_indexer.py --search "白板の戦略案"
  python3 scripts/multimodal_indexer.py --list
  python3 scripts/multimodal_indexer.py --stats
"""
from __future__ import annotations

import argparse
import hashlib
import json
import logging
import os
import sys
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import Any

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger("multimodal_indexer")

APP_ROOT = Path(os.getenv("BRAIN_APP_ROOT", "/app"))
DATA_BRAIN = APP_ROOT / "data" / "brain"
MULTIMODAL_DIR = DATA_BRAIN / "multimodal"
INDEX_PATH = MULTIMODAL_DIR / "index.jsonl"

# soft import: CLIP は重いので Phase 1 ではオプショナル
try:
    import numpy as np  # type: ignore
    _NUMPY_AVAILABLE = True
except Exception:
    _NUMPY_AVAILABLE = False
    np = None  # type: ignore

try:
    from sentence_transformers import SentenceTransformer  # type: ignore
    _SBERT_AVAILABLE = True
except Exception:
    _SBERT_AVAILABLE = False
    SentenceTransformer = None  # type: ignore


# ─── ノード型 ────────────────────────────────
@dataclass
class MultimodalRecord:
    """1 つの画像/音声を表す index record。"""
    id: str                    # sha256 short (12 chars)
    path: str                  # 元 file path (絶対 or 相対)
    media_type: str            # image | audio | video
    caption: str               # 人間可読 description
    ocr_text: str = ""         # OCR で取れた text (image の場合)
    transcript: str = ""       # 音声 transcript (audio の場合)
    width: int | None = None
    height: int | None = None
    bytes_size: int | None = None
    embedding: list[float] | None = None   # CLIP / sbert (Phase 2)
    indexed_at: str = ""
    tags: list[str] = field(default_factory=list)


# ─── index 読み書き ─────────────────────────
def _ensure_dir():
    MULTIMODAL_DIR.mkdir(parents=True, exist_ok=True)


def load_index() -> list[MultimodalRecord]:
    if not INDEX_PATH.exists():
        return []
    out = []
    for line in INDEX_PATH.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        try:
            d = json.loads(line)
            out.append(MultimodalRecord(**d))
        except Exception as e:
            logger.warning(f"parse error: {e}")
    return out


def save_index(records: list[MultimodalRecord]) -> None:
    _ensure_dir()
    with INDEX_PATH.open("w", encoding="utf-8") as f:
        for r in records:
            f.write(json.dumps(asdict(r), ensure_ascii=False) + "\n")


def add_to_index(record: MultimodalRecord) -> bool:
    """既存 record (同 id) があれば更新、なければ追加。"""
    records = load_index()
    existing_ids = {r.id: i for i, r in enumerate(records)}
    if record.id in existing_ids:
        records[existing_ids[record.id]] = record
    else:
        records.append(record)
    save_index(records)
    return True


# ─── 1 file を index に追加 ──────────────────
def _sha256_id(path: Path) -> str:
    h = hashlib.sha256(path.read_bytes()).hexdigest()
    return h[:12]


def _detect_media_type(path: Path) -> str:
    suf = path.suffix.lower()
    if suf in (".jpg", ".jpeg", ".png", ".gif", ".webp", ".bmp", ".heic"):
        return "image"
    if suf in (".mp3", ".wav", ".m4a", ".flac", ".ogg"):
        return "audio"
    if suf in (".mp4", ".mov", ".avi", ".mkv", ".webm"):
        return "video"
    return "unknown"


def index_file(
    path: Path,
    caption: str = "",
    ocr_text: str = "",
    transcript: str = "",
    tags: list[str] | None = None,
    compute_embedding: bool = False,
) -> MultimodalRecord:
    """1 file を index に追加。

    compute_embedding=True なら sentence-transformers で caption を embed
    (Phase 2 で CLIP に差し替え予定)。
    """
    if not path.exists():
        raise SystemExit(f"file not found: {path}")
    media_type = _detect_media_type(path)
    bytes_size = path.stat().st_size
    rec = MultimodalRecord(
        id=_sha256_id(path),
        path=str(path),
        media_type=media_type,
        caption=caption,
        ocr_text=ocr_text,
        transcript=transcript,
        bytes_size=bytes_size,
        indexed_at=datetime.now(timezone(timedelta(hours=9))).isoformat(),
        tags=tags or [],
    )
    if compute_embedding and _SBERT_AVAILABLE and caption:
        try:
            model = SentenceTransformer("paraphrase-multilingual-MiniLM-L12-v2")
            emb = model.encode(caption).tolist()
            rec.embedding = emb
        except Exception as e:
            logger.warning(f"embedding 計算失敗: {e}")
    add_to_index(rec)
    return rec


# ─── 検索 ────────────────────────────────────
def search_by_caption_substring(query: str, limit: int = 10) -> list[MultimodalRecord]:
    """caption / ocr_text / transcript / tags の substring search (embedding 無し fallback)。"""
    records = load_index()
    q = query.lower()
    hits = []
    for r in records:
        haystack = " ".join([
            r.caption.lower(),
            r.ocr_text.lower(),
            r.transcript.lower(),
            " ".join(r.tags).lower(),
        ])
        if q in haystack:
            hits.append(r)
    return hits[:limit]


def search_by_embedding(query: str, limit: int = 10) -> list[tuple[MultimodalRecord, float]]:
    """sentence-transformers embedding cosine similarity 検索 (Phase 2 で CLIP)。

    sentence-transformers が無ければ caption substring に fallback。
    """
    if not _SBERT_AVAILABLE or not _NUMPY_AVAILABLE:
        # fallback to substring
        hits = search_by_caption_substring(query, limit)
        return [(h, 0.0) for h in hits]

    records = [r for r in load_index() if r.embedding]
    if not records:
        return []
    try:
        model = SentenceTransformer("paraphrase-multilingual-MiniLM-L12-v2")
        q_emb = model.encode(query)
        scored = []
        for r in records:
            r_emb = np.array(r.embedding)  # type: ignore
            # cosine sim
            sim = float((q_emb @ r_emb) /
                        (np.linalg.norm(q_emb) * np.linalg.norm(r_emb) + 1e-8))  # type: ignore
            scored.append((r, sim))
        scored.sort(key=lambda x: -x[1])
        return scored[:limit]
    except Exception as e:
        logger.warning(f"embedding search 失敗、substring fallback: {e}")
        hits = search_by_caption_substring(query, limit)
        return [(h, 0.0) for h in hits]


# ─── stats / CLI ─────────────────────────────
def stats() -> dict:
    records = load_index()
    by_type: dict[str, int] = {}
    n_with_embedding = 0
    total_bytes = 0
    for r in records:
        by_type[r.media_type] = by_type.get(r.media_type, 0) + 1
        if r.embedding:
            n_with_embedding += 1
        if r.bytes_size:
            total_bytes += r.bytes_size
    return {
        "n_records": len(records),
        "by_media_type": by_type,
        "n_with_embedding": n_with_embedding,
        "total_bytes": total_bytes,
        "sbert_available": _SBERT_AVAILABLE,
        "numpy_available": _NUMPY_AVAILABLE,
    }


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--add", help="file path を index に追加")
    ap.add_argument("--caption", default="")
    ap.add_argument("--ocr-text", default="")
    ap.add_argument("--transcript", default="")
    ap.add_argument("--tags", default="", help="comma-separated")
    ap.add_argument("--with-embedding", action="store_true")
    ap.add_argument("--search", help="text query で検索")
    ap.add_argument("--embedding-search", action="store_true",
                    help="cosine sim 検索 (sentence-transformers あれば)")
    ap.add_argument("--limit", type=int, default=10)
    ap.add_argument("--list", action="store_true", help="全 record")
    ap.add_argument("--stats", action="store_true")
    args = ap.parse_args()

    if args.add:
        rec = index_file(
            Path(args.add),
            caption=args.caption,
            ocr_text=args.ocr_text,
            transcript=args.transcript,
            tags=[t.strip() for t in args.tags.split(",") if t.strip()],
            compute_embedding=args.with_embedding,
        )
        print(json.dumps(asdict(rec), ensure_ascii=False, indent=2))
        return 0

    if args.search:
        if args.embedding_search:
            results = search_by_embedding(args.search, limit=args.limit)
            for r, sim in results:
                print(f"[{sim:.3f}] {r.id} {r.path} — {r.caption}")
        else:
            hits = search_by_caption_substring(args.search, limit=args.limit)
            for r in hits:
                print(f"{r.id} {r.path} — {r.caption}")
        return 0

    if args.list:
        for r in load_index():
            print(f"{r.id} [{r.media_type}] {r.path} — {r.caption}")
        return 0

    if args.stats:
        print(json.dumps(stats(), ensure_ascii=False, indent=2))
        return 0

    ap.print_help()
    return 0


if __name__ == "__main__":
    sys.exit(main())
