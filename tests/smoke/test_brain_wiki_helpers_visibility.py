"""smoke test: brain_wiki_helpers/visibility.py (★2026-05-22 Phase 1a 切り出し)。

Pure function なので直接 import + test 可能 (= brain_wiki.py の重い依存ナシ)。
"""
from __future__ import annotations

from datetime import date, timedelta

import pytest

from brain_wiki_helpers.visibility import parse_clone_visibility, parse_is_retired


@pytest.mark.smoke
def test_visibility_public():
    content = "---\nclone_visibility: public\nexit_visibility: public\n---\n# body\n"
    assert parse_clone_visibility(content) == "public"


@pytest.mark.smoke
def test_visibility_private():
    content = "---\nclone_visibility: private\n---\n# body\n"
    assert parse_clone_visibility(content) == "private"


@pytest.mark.smoke
def test_visibility_missing_defaults_private():
    """clone_visibility 未設定なら private (fail-safe)。"""
    content = "---\nexit_visibility: public\n---\n# body\n"
    assert parse_clone_visibility(content) == "private"


@pytest.mark.smoke
def test_visibility_no_frontmatter_private():
    content = "# 本文のみ\nstyle 例\n"
    assert parse_clone_visibility(content) == "private"


@pytest.mark.smoke
def test_retired_via_superseded_by():
    content = (
        f"---\nclone_visibility: public\nsuperseded_by: style-new-v2\n"
        f"last_updated: {date.today().isoformat()}\n---\n# body\n"
    )
    assert parse_is_retired(content) is True


@pytest.mark.smoke
def test_retired_via_valid_until_past():
    past = (date.today() - timedelta(days=10)).isoformat()
    content = (
        f"---\nclone_visibility: public\nvalid_until: {past}\n---\n# body\n"
    )
    assert parse_is_retired(content) is True


@pytest.mark.smoke
def test_not_retired_when_valid_until_future():
    future = (date.today() + timedelta(days=30)).isoformat()
    content = (
        f"---\nclone_visibility: public\nvalid_until: {future}\n---\n# body\n"
    )
    assert parse_is_retired(content) is False


@pytest.mark.smoke
def test_not_retired_when_no_markers():
    content = (
        f"---\nclone_visibility: public\n"
        f"last_updated: {date.today().isoformat()}\n---\n# body\n"
    )
    assert parse_is_retired(content) is False


@pytest.mark.smoke
def test_not_retired_when_empty_superseded_by():
    """空の superseded_by は立ってない扱い。"""
    content = (
        f"---\nclone_visibility: public\nsuperseded_by:\n"
        f"last_updated: {date.today().isoformat()}\n---\n# body\n"
    )
    assert parse_is_retired(content) is False


@pytest.mark.smoke
def test_visibility_with_extra_whitespace():
    """tab / 余分な space でも parse できる。"""
    content = "---\nclone_visibility:    public   \n---\n# body\n"
    assert parse_clone_visibility(content) == "public"
