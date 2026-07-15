"""smoke test: multimodal_indexer (Phase 1 skeleton)."""
from __future__ import annotations

import importlib
import json
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(REPO_ROOT / "scripts"))


@pytest.fixture
def mm(tmp_path, monkeypatch):
    """multimodal_indexer を BRAIN_APP_ROOT=tmp で reload。"""
    monkeypatch.setenv("BRAIN_APP_ROOT", str(tmp_path))
    if "multimodal_indexer" in sys.modules:
        importlib.reload(sys.modules["multimodal_indexer"])
    import multimodal_indexer as mod  # type: ignore
    return mod, tmp_path


@pytest.fixture
def sample_image(tmp_path):
    """偽の jpg ファイル (中身は適当な bytes)。"""
    p = tmp_path / "sample.jpg"
    p.write_bytes(b"\xff\xd8\xff\xe0fake-jpg-content")  # JPEG magic header だけ
    return p


@pytest.mark.smoke
def test_module_imports(mm):
    mod, _ = mm
    assert hasattr(mod, "index_file")
    assert hasattr(mod, "search_by_caption_substring")
    assert hasattr(mod, "search_by_embedding")
    assert hasattr(mod, "load_index")
    assert hasattr(mod, "MultimodalRecord")


@pytest.mark.smoke
def test_detect_media_type(mm):
    mod, _ = mm
    assert mod._detect_media_type(Path("a.jpg")) == "image"
    assert mod._detect_media_type(Path("b.PNG")) == "image"
    assert mod._detect_media_type(Path("c.mp3")) == "audio"
    assert mod._detect_media_type(Path("d.mp4")) == "video"
    assert mod._detect_media_type(Path("e.txt")) == "unknown"


@pytest.mark.smoke
def test_index_file_creates_record(mm, sample_image):
    mod, _ = mm
    rec = mod.index_file(sample_image, caption="テスト画像", tags=["test"])
    assert rec.media_type == "image"
    assert rec.caption == "テスト画像"
    assert "test" in rec.tags
    assert len(rec.id) == 12  # sha256 short
    assert rec.bytes_size > 0


@pytest.mark.smoke
def test_load_index_after_add(mm, sample_image):
    mod, _ = mm
    mod.index_file(sample_image, caption="img-1")
    records = mod.load_index()
    assert len(records) == 1
    assert records[0].caption == "img-1"


@pytest.mark.smoke
def test_add_same_file_updates(mm, sample_image):
    """同じ file (= 同じ sha256 id) は update する、重複しない。"""
    mod, _ = mm
    mod.index_file(sample_image, caption="img-1")
    mod.index_file(sample_image, caption="img-2-updated")
    records = mod.load_index()
    assert len(records) == 1
    assert records[0].caption == "img-2-updated"


@pytest.mark.smoke
def test_search_by_caption_substring(mm, tmp_path):
    """caption / ocr_text / tags で substring 検索。"""
    mod, _ = mm
    # 3 つの偽 record
    for i, (path_name, caption, ocr, tags) in enumerate([
        ("a.jpg", "VMV 会議の白板", "シンプル・クイック・バリュアブル", ["meeting"]),
        ("b.jpg", "店舗外観", "OWNDAYS", ["store"]),
        ("c.mp3", "雑談録音", "", ["voice"]),
    ]):
        p = tmp_path / path_name
        p.write_bytes(f"fake-{i}".encode())
        mod.index_file(p, caption=caption, ocr_text=ocr, tags=tags)

    hits = mod.search_by_caption_substring("白板")
    assert len(hits) == 1
    assert hits[0].caption == "VMV 会議の白板"

    # OCR text にもヒット
    hits2 = mod.search_by_caption_substring("シンプル")
    assert len(hits2) == 1

    # tag にもヒット
    hits3 = mod.search_by_caption_substring("voice")
    assert len(hits3) == 1
    assert hits3[0].media_type == "audio"


@pytest.mark.smoke
def test_search_returns_empty_on_no_match(mm, sample_image):
    mod, _ = mm
    mod.index_file(sample_image, caption="img-x")
    hits = mod.search_by_caption_substring("存在しないキーワード123")
    assert hits == []


@pytest.mark.smoke
def test_stats_structure(mm, sample_image):
    """stats() が n_records / by_media_type / sbert_available を返す。"""
    mod, _ = mm
    mod.index_file(sample_image, caption="img-1")
    s = mod.stats()
    assert s["n_records"] == 1
    assert s["by_media_type"]["image"] == 1
    # availability flag が bool
    assert isinstance(s["sbert_available"], bool)
    assert isinstance(s["numpy_available"], bool)


@pytest.mark.smoke
def test_search_by_embedding_fallback(mm, sample_image):
    """sentence-transformers 無しなら substring fallback。"""
    mod, _ = mm
    mod.index_file(sample_image, caption="VMV 会議")
    results = mod.search_by_embedding("VMV", limit=5)
    # sbert あれば cosine + tuple、無ければ substring + 0.0
    assert len(results) >= 1
    rec, score = results[0]
    assert rec.caption == "VMV 会議"


@pytest.mark.smoke
def test_index_path_is_under_brain_root(mm, sample_image):
    """INDEX_PATH が tmp BRAIN_APP_ROOT 配下にある。"""
    mod, tmp_path = mm
    mod.index_file(sample_image, caption="x")
    assert mod.INDEX_PATH.exists()
    assert str(mod.INDEX_PATH).startswith(str(tmp_path))


@pytest.mark.smoke
def test_video_file_detection(mm, tmp_path):
    """.mp4 が video として認識される。"""
    mod, _ = mm
    p = tmp_path / "meeting.mp4"
    p.write_bytes(b"fake-mp4")
    rec = mod.index_file(p, caption="会議録画", transcript="...")
    assert rec.media_type == "video"
    assert rec.transcript == "..."
