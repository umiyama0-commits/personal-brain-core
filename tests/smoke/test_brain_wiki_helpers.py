"""smoke test: brain_wiki.py の retrieval ヘルパー (vector search 不要なロジック単体)。

vector search / Chroma 系は重いので別 integration test に回す。
ここは正規表現 / 文字列処理 / 設定リスト等の純粋ロジックのみ。
"""
from __future__ import annotations

import re
import pytest


@pytest.mark.smoke
def test_prefecture_keywords_contains_major():
    """都道府県 keyword list が主要県を含む (brain_wiki.py の hardcoded list)。"""
    # brain_wiki の中で hardcode された list を直接 import で取れないので
    # 当該 list が含むべき必須要素のスモークテスト (検出ロジックの基盤確認)
    must = ["東京", "大阪", "愛知", "福岡", "沖縄", "神奈川"]
    # source code から list 抽出 (regex で行頭の "PREFECTURE_KEYWORDS = (" を見つける)
    from pathlib import Path
    src = (Path(__file__).resolve().parent.parent.parent / "brain_wiki.py").read_text(encoding="utf-8")
    m = re.search(r"PREFECTURE_KEYWORDS\s*=\s*\(([^)]+)\)", src, re.DOTALL)
    assert m, "PREFECTURE_KEYWORDS が brain_wiki.py に無い"
    body = m.group(1)
    for pref in must:
        assert pref in body, f"PREFECTURE_KEYWORDS に {pref} が含まれてない"


@pytest.mark.smoke
def test_am_keywords_contains_6_managers():
    """AM 6 名のフルネームが AM_KEYWORDS に含まれる。"""
    from pathlib import Path
    src = (Path(__file__).resolve().parent.parent.parent / "brain_wiki.py").read_text(encoding="utf-8")
    m = re.search(r"AM_KEYWORDS\s*=\s*\(([^)]+)\)", src, re.DOTALL)
    assert m
    body = m.group(1)
    # store-master.md にある 6 AM
    must = ["谷口里美", "中田将也", "渡邉俊也", "田口裕一朗", "熊野篤", "平林真之"]
    for name in must:
        assert name in body, f"AM_KEYWORDS に {name} が含まれてない"


@pytest.mark.smoke
def test_core_files_truncated_has_essential():
    """core retrieval (常時 context) に必須 wiki が含まれてること。"""
    from pathlib import Path
    src = (Path(__file__).resolve().parent.parent.parent / "brain_wiki.py").read_text(encoding="utf-8")
    m = re.search(r"core_files_truncated\s*=\s*\[(.*?)\]", src, re.DOTALL)
    assert m
    body = m.group(1)
    essentials = [
        "identity.md",
        "style.md",
        "thinking.md",
        "knowledge/owndays-vmv.md",
        "knowledge/owndays-organization.md",
        # 2026-05-19 追加
        "style/style-no-claude-proposals.md",
        "style/response-bank.md",
        "knowledge/clone-disclosure-policy.md",
        # 2026-05-20 追加
        "style/style-depth-as-undercurrent.md",
    ]
    for path in essentials:
        assert path in body, f"core_files_truncated に {path} が含まれてない"


@pytest.mark.smoke
def test_relative_date_phrases_in_retrieval():
    """相対日付 (昨日 / 先週 / 一昨日) が brain_wiki.py で扱えること。"""
    from pathlib import Path
    src = (Path(__file__).resolve().parent.parent.parent / "brain_wiki.py").read_text(encoding="utf-8")
    # CLAUDE.md に書かれてる主要相対日付
    must = ["昨日", "一昨日", "先週", "今週"]
    for kw in must:
        assert kw in src, f"brain_wiki.py に {kw} の処理が見当たらない"
