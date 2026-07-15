"""tests/smoke/test_brand_canonical.py — 自社名の綴り事故を決定論矯正 (★QA 2026-07-12).

GPT-5.4 が稀に「OWDAYS」(N抜け)・「OWNDAY」(S抜け) を生成 → CEO クローンが自社名を
間違える信頼毀損を防ぐ。全 clone 応答に _fac_guard 経由で適用。
"""
from __future__ import annotations

import brain_wiki as bw


def test_canonicalize_fixes_known_typos():
    assert bw._canonicalize_brand("OWDAYSの社内情報") == "OWNDAYSの社内情報"
    assert bw._canonicalize_brand("OWNDAY で働く") == "OWNDAYS で働く"
    assert bw._canonicalize_brand("OWNADYS も") == "OWNDAYS も"


def test_canonicalize_leaves_correct_untouched():
    assert bw._canonicalize_brand("OWNDAYS は正常") == "OWNDAYS は正常"
    assert bw._canonicalize_brand("OWNDAYS船橋店とOWNDAYS岡崎店") == "OWNDAYS船橋店とOWNDAYS岡崎店"
    assert bw._canonicalize_brand("普通の日本語文") == "普通の日本語文"
    assert bw._canonicalize_brand("") == ""


def test_canonicalize_wired_into_fac_guard():
    """clone_respond_public の _fac_guard が社名矯正を全応答に噛ませていること (source pin)。"""
    from pathlib import Path
    src = (Path(__file__).resolve().parents[2] / "brain_wiki.py").read_text(encoding="utf-8")
    assert "text_out = _canonicalize_brand(text_out)" in src
