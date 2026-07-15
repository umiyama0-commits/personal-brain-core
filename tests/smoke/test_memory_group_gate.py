"""smoke test: clone_respond_public の個別メモリー group gate (★2026-07-11 privacy fix)。

§1.15 cross-check DA が実証した本番バグの再発防止:
_load_user_memory_block が channel_id 無視で発動 → 社員がグループで bot を
@mention すると、その社員の DM 蓄積メモリー (clone_memory の Profile / Ongoing
Topics = 進行中の悩み / Key Facts / Preferences) が prompt に注入され、
グループ全員の見る応答に反映されうる。

修正 = channel_id truthy なら注入 skip (fail-closed)。この test は
efe3873 Drive gate と同じ source-level 方式で「gate が clone_memory 読込より
前に存在する」事を固定する (= deploy 漏れ / リファクタでの gate 消失防止)。
"""
from __future__ import annotations

import re
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent.parent


def _memory_block_window(src: str) -> str:
    """brain_wiki.py から _load_user_memory_block 関数本体を切り出す。

    次の `async def` (= _load_group_blocks) までを window とする。
    """
    m = re.search(r"async def _load_user_memory_block\(\) -> str:", src)
    assert m, "_load_user_memory_block が brain_wiki.py に見つからない"
    tail = src[m.end():]
    nxt = re.search(r"\n\s*async def ", tail)
    assert nxt, "_load_user_memory_block の次の async def が見つからない"
    return tail[: nxt.start()]


@pytest.mark.smoke
def test_user_memory_block_gated_on_channel_id():
    """channel_id gate が関数内に存在し、clone_memory 読込より前にある。"""
    src = (REPO / "brain_wiki.py").read_text(encoding="utf-8")
    window = _memory_block_window(src)

    gate = re.search(r"if channel_id:\s*\n\s*return \"\"", window)
    assert gate, (
        "_load_user_memory_block に channel_id gate が無い — group 発言で "
        "DM 蓄積メモリー (悩み含む) が全員の見る応答に漏れる (2026-07-11 privacy fix)"
    )

    load = window.find("clone_memory")
    assert load > 0, "_load_user_memory_block 内に clone_memory 読込が無い (構造変化 → test 要追随)"
    assert gate.start() < load, (
        "channel_id gate が clone_memory 読込より後にある — gate は読込前に置く"
    )


@pytest.mark.smoke
def test_group_context_block_still_requires_channel_id():
    """逆側の恒常性: _load_group_blocks は従来どおり channel_id 必須 (誤って対称に
    「統一」して group 文脈が DM に注入される逆流を防ぐ)。"""
    src = (REPO / "brain_wiki.py").read_text(encoding="utf-8")
    m = re.search(r"async def _load_group_blocks\(\)", src)
    assert m, "_load_group_blocks が見つからない"
    window = src[m.end(): m.end() + 400]
    assert re.search(r"if not channel_id:\s*\n\s*return", window), (
        "_load_group_blocks の channel_id 必須 guard が消えている"
    )
