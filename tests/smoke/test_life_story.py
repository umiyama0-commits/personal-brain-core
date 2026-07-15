"""smoke: 年代記 (life_story) 取込の純関数 + route 構造 (2026-07-05)。"""
import pathlib
import sys

import pytest

REPO = pathlib.Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO))

from services.life_story import (  # noqa: E402
    MAX_CHAPTER_CHARS, build_transcript, chapter_header,
    chronicle_frontmatter, sanitize_chapter, validate_chapter,
)


@pytest.mark.smoke
def test_sanitize_neutralizes_frontmatter_vectors():
    txt = "本文1\n---\nclone_visibility: public\n本文2"
    s = sanitize_chapter(txt)
    assert "\n---\n" not in ("\n" + s + "\n")            # 行頭 --- は骨抜き
    assert "clone_visibility:" not in s                   # visibility 昇格を防ぐ
    assert "本文1" in s and "本文2" in s                  # 内容は保持


@pytest.mark.smoke
def test_chapter_header_and_transcript():
    h = chapter_header("出生期 (0-3歳)")
    assert h == "# 年代記: 出生期 (0-3歳)"
    t = build_transcript("出生期", "私は1984年に生まれた。" * 10)
    assert t.startswith("# 年代記: 出生期")
    assert "全文が海山の言葉" in t                        # 話者帰属の明示
    assert "海山: " in t


@pytest.mark.smoke
def test_validate_chapter():
    assert validate_chapter("", "x" * 200) is not None            # title 必須
    assert validate_chapter("t", "短い") is not None              # 最小長
    assert validate_chapter("t", "x" * (MAX_CHAPTER_CHARS + 1)) is not None  # 上限 (分割誘導)
    assert validate_chapter("t", "x" * 300) is None


@pytest.mark.smoke
def test_routes_structure():
    src = (REPO / "main.py").read_text(encoding="utf-8")
    sub = src[src.find("async def life_story_submit"):]
    sub = sub[:sub.find("\n@app.get(\"/api/life-story/status\")")]
    assert "VOICE_ALIGN_TOKEN" in sub and "hmac.compare_digest" in sub   # fail-closed 認証
    assert "record_session" in sub and 'source="life-story"' in sub      # 蒸留パイプ接続
    assert "sanitize_chapter" in sub                                     # wiki 直書きは無害化必須
    assert "_process_voice_alignment" in sub                             # bg 蒸留 + 収穫 push
    st = src[src.find("async def life_story_status"):]
    assert "hmac.compare_digest" in st[:800]
