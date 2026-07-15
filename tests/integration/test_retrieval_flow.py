"""
Integration test: retrieval flow (visibility / retired / scrub の連携)。

brain_wiki.py の重い import は避け、retrieval ロジックの core (visibility
parse + retired 検出 + scrub) を source 抽出 + exec で隔離 test。
"""
from __future__ import annotations

import re
import sys
import textwrap
from datetime import date, timedelta
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent.parent


def _extract_static_method(src: str, method_name: str) -> str:
    """brain_wiki.py から @staticmethod def method_name(...) の関数を抜く。

    test_drift_bitemporal の手法と同じ:
    - `    @staticmethod\n    def <name>(` の開始を探す
    - 次の同インデント (4-space) の def or @staticmethod または class で終端
    - @staticmethod を剥がして free 関数化、dedent
    """
    start = src.find(f"    @staticmethod\n    def {method_name}(")
    assert start >= 0, f"not found: @staticmethod ... def {method_name}"
    # @staticmethod 行をスキップして関数本体に進む
    rest = src[start + len("    @staticmethod\n"):]
    # 次の同レベル method 開始 or class までを探す
    end_m = re.search(
        r"\n    @staticmethod\n|\n    def [a-zA-Z_]|\nclass |\ndef ",
        rest,
    )
    assert end_m, f"function end not detected for {method_name}"
    func_src_raw = "    @staticmethod\n" + rest[: end_m.start() + 1]
    return textwrap.dedent(func_src_raw).replace("@staticmethod\n", "")


@pytest.fixture
def helpers():
    """brain_wiki から visibility / retired parse の関数を抜き出す。"""
    src = (REPO_ROOT / "brain_wiki.py").read_text(encoding="utf-8")
    vis_src = _extract_static_method(src, "_parse_clone_visibility")
    ret_src = _extract_static_method(src, "_parse_is_retired")

    ns: dict = {}
    exec(vis_src, {"re": re}, ns)
    exec(ret_src, {"re": re}, ns)
    return {
        "parse_visibility": ns["_parse_clone_visibility"],
        "parse_retired": ns["_parse_is_retired"],
    }


@pytest.mark.integration
def test_visibility_public_retrieval_eligible(helpers):
    """clone_visibility: public な記事は retrieval 対象。"""
    content = "---\nclone_visibility: public\nexit_visibility: public\n---\n# body\n"
    assert helpers["parse_visibility"](content) == "public"


@pytest.mark.integration
def test_visibility_private_default_when_missing(helpers):
    """clone_visibility 未設定なら private (fail-safe)。"""
    content = "---\nexit_visibility: public\n---\n# body\n"
    assert helpers["parse_visibility"](content) == "private"


@pytest.mark.integration
def test_visibility_fallback_no_frontmatter(helpers):
    """frontmatter 自体が無い記事は private。"""
    content = "# 本文だけ\nstyle 例\n"
    assert helpers["parse_visibility"](content) == "private"


@pytest.mark.integration
def test_retired_via_superseded_by(helpers):
    """superseded_by 立ってる記事は retired。"""
    content = (
        "---\nclone_visibility: public\nsuperseded_by: style-new-v2\n"
        f"last_updated: {date.today().isoformat()}\n---\n# body\n"
    )
    assert helpers["parse_retired"](content) is True


@pytest.mark.integration
def test_retired_via_valid_until_past(helpers):
    """valid_until が今日以前なら retired。"""
    past = (date.today() - timedelta(days=30)).isoformat()
    content = (
        f"---\nclone_visibility: public\nvalid_until: {past}\n"
        f"last_updated: {date.today().isoformat()}\n---\n# body\n"
    )
    assert helpers["parse_retired"](content) is True


@pytest.mark.integration
def test_retired_via_valid_until_future_still_active(helpers):
    """valid_until が未来日付ならまだ active (retired じゃない)。"""
    future = (date.today() + timedelta(days=30)).isoformat()
    content = (
        f"---\nclone_visibility: public\nvalid_until: {future}\n"
        f"last_updated: {date.today().isoformat()}\n---\n# body\n"
    )
    assert helpers["parse_retired"](content) is False


@pytest.mark.integration
def test_retired_combo_visibility_public_but_retired(helpers):
    """clone_visibility=public でも superseded_by 立ってれば retired。

    実際の retrieval では visibility=public でも retired なら除外される設計。
    """
    content = (
        "---\nclone_visibility: public\nsuperseded_by: style-newer\n---\n# body\n"
    )
    assert helpers["parse_visibility"](content) == "public"
    assert helpers["parse_retired"](content) is True


@pytest.mark.integration
def test_scrub_removes_private_wikilink_line():
    """scrub() のロジック: [[private/x]] を含む行が丸ごと削除される。

    brain_wiki._read_wiki_state_public_compact 内の scrub() を簡易再現。
    """
    private_paths = {"people/sample-tencho", "people/sample-tencho.md"}

    def scrub(text: str) -> str:
        out_lines = []
        for line in text.splitlines():
            leaked = False
            for m in re.finditer(r"\[\[([^\]|]+?)(?:\|[^\]]*)?\]\]", line):
                target = m.group(1).strip()
                if target in private_paths:
                    leaked = True
                    break
            if not leaked:
                out_lines.append(line)
        return "\n".join(out_lines)

    src = (
        "店長: 見本 太郎\n"
        "| 見本 太郎 | [[people/sample-tencho]] |\n"  # 行ごと消える
        "売上 100M\n"
    )
    out = scrub(src)
    assert "見本 太郎" in out  # display text の名前は別行に残る (例: 「店長: 見本 太郎」)
    assert "[[people/sample-tencho]]" not in out
    assert "売上 100M" in out
