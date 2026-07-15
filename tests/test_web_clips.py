"""tests/test_web_clips.py — Web 取込 dashboard tests

★2026-05-26 海山指示「web 等で拾ってきた考え方や言葉を自分の考えの wiki に反映」:
ダッシュボードから quote + 感想 + 反映先 を入力 → pending queue → review → wiki 追記。
"""
from __future__ import annotations

import importlib
import json
import sys
import time
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))


@pytest.fixture
def tmp_brain(tmp_path, monkeypatch):
    monkeypatch.setenv("BRAIN_ROOT", str(tmp_path))
    if "services.web_clips" in sys.modules:
        del sys.modules["services.web_clips"]
    mod = importlib.import_module("services.web_clips")
    mod.CLIPS_DIR = tmp_path / "clone_review"
    mod.CLIPS_FILE = mod.CLIPS_DIR / "web_clips.jsonl"
    mod.WIKI_DIR = tmp_path / "wiki"
    return mod


def test_add_clip_basic(tmp_brain):
    mod = tmp_brain
    cid = mod.add_clip(
        quote="本当の信仰とは強いられないこと",
        target_wiki="interview/value-roots.md",
        title="Naval - 信仰",
        source_url="https://nav.al/x",
        reflection="共感",
    )
    assert cid.startswith("clip_")
    pending = mod.list_pending()
    assert len(pending) == 1
    p = pending[0]
    assert p["title"] == "Naval - 信仰"
    assert p["status"] == "pending"
    assert p["target_wiki"] == "interview/value-roots.md"


def test_add_clip_requires_quote_and_valid_target(tmp_brain):
    mod = tmp_brain
    with pytest.raises(ValueError):
        mod.add_clip(quote="", target_wiki="interview/biography.md")
    with pytest.raises(ValueError):
        mod.add_clip(quote="x", target_wiki="/etc/passwd")
    with pytest.raises(ValueError):
        mod.add_clip(quote="x", target_wiki="arbitrary/path.md")


def test_apply_clip_creates_wiki(tmp_brain):
    mod = tmp_brain
    cid = mod.add_clip(
        quote="人は信じたいものを信じる",
        target_wiki="interview/aesthetics.md",
        title="美意識の話",
        reflection="これは自分も感じる",
    )
    result = mod.apply_clip(cid)
    assert result["ok"], result

    target = mod.WIKI_DIR / "interview/aesthetics.md"
    assert target.exists()
    content = target.read_text(encoding="utf-8")
    assert "美意識の話" in content
    assert "> 人は信じたいものを信じる" in content
    assert "**海山の感想**: これは自分も感じる" in content
    # frontmatter (= 新規生成時)
    assert "clone_visibility: private" in content


def test_apply_clip_appends_existing_wiki(tmp_brain):
    mod = tmp_brain
    # 既存 wiki ファイル作成
    target = mod.WIKI_DIR / "interview/value-roots.md"
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(
        "---\nupdated: 2026-05-25\nclone_visibility: private\n---\n# Existing\n\nold content\n",
        encoding="utf-8",
    )

    cid = mod.add_clip(quote="new quote", target_wiki="interview/value-roots.md")
    result = mod.apply_clip(cid)
    assert result["ok"]

    content = target.read_text(encoding="utf-8")
    assert "old content" in content
    assert "> new quote" in content


def test_apply_already_applied(tmp_brain):
    mod = tmp_brain
    cid = mod.add_clip(quote="x", target_wiki="interview/biography.md")
    mod.apply_clip(cid)
    result = mod.apply_clip(cid)
    assert not result["ok"]
    assert "already" in result["error"]


def test_update_status_and_count_pending(tmp_brain):
    mod = tmp_brain
    cid1 = mod.add_clip(quote="x1", target_wiki="interview/biography.md")
    time.sleep(0.01)
    cid2 = mod.add_clip(quote="x2", target_wiki="interview/biography.md")
    assert mod.count_pending() == 2
    mod.update_status(cid1, "rejected")
    assert mod.count_pending() == 1


def test_update_clip(tmp_brain):
    mod = tmp_brain
    cid = mod.add_clip(quote="original", target_wiki="interview/biography.md", title="orig")
    ok = mod.update_clip(cid, title="updated", quote="new quote")
    assert ok
    c = mod.find_clip(cid)
    assert c["title"] == "updated"
    assert c["quote"] == "new quote"

    # invalid target_wiki should be silently skipped
    mod.update_clip(cid, target_wiki="/etc/passwd")
    c = mod.find_clip(cid)
    assert c["target_wiki"] == "interview/biography.md"  # 元のまま

    # applied は edit 不可
    mod.update_status(cid, "applied")
    ok2 = mod.update_clip(cid, title="should not change")
    assert not ok2


def test_add_comment(tmp_brain):
    mod = tmp_brain
    cid = mod.add_clip(quote="x", target_wiki="interview/biography.md")
    ok = mod.add_comment(cid, "後で再 review")
    assert ok
    c = mod.find_clip(cid)
    assert len(c["comments"]) == 1
    assert c["comments"][0]["comment"] == "後で再 review"


def test_render_web_clip_page_no_data(tmp_brain):
    from services.review_dashboard import render_web_clip_page
    html = render_web_clip_page("test-token")
    assert "Web 取込" in html
    assert "action-form" in html
    assert 'name="quote"' in html
    assert 'name="target_wiki"' in html
    # WIKI_TARGETS option が出てる
    assert "interview/biography.md" in html
    assert "interview/value-roots.md" in html


def test_render_web_clip_page_with_pending(tmp_brain):
    mod = tmp_brain
    mod.add_clip(
        quote="sample quote",
        target_wiki="interview/biography.md",
        title="test title",
        reflection="my note",
    )
    from services.review_dashboard import render_web_clip_page
    html = render_web_clip_page("test-token")
    assert "test title" in html
    assert "sample quote" in html
    assert "my note" in html
    assert "name=\"action\" value=\"apply\"" in html


def test_handle_action_apply(tmp_brain):
    mod = tmp_brain
    cid = mod.add_clip(quote="x", target_wiki="interview/biography.md", title="t")
    from services.review_dashboard import handle_web_clip_action
    ok, msg = handle_web_clip_action("apply", cid)
    assert ok
    assert "wiki" in msg.lower() and "反映" in msg


def test_handle_action_reject(tmp_brain):
    mod = tmp_brain
    cid = mod.add_clip(quote="x", target_wiki="interview/biography.md")
    from services.review_dashboard import handle_web_clip_action
    ok, msg = handle_web_clip_action("reject", cid, note="不要")
    assert ok
    assert "rejected" in msg


def test_handle_action_unknown(tmp_brain):
    from services.review_dashboard import handle_web_clip_action
    ok, msg = handle_web_clip_action("destroy", "any")
    assert not ok
    assert "unknown" in msg


def test_nav_includes_web_clip():
    from services.review_dashboard import _nav
    html = _nav("/admin/review/learning", "test-token")
    assert "/admin/review/web-clip" in html
    assert "Web 取込" in html
