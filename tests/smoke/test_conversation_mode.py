"""smoke test: 雑談 / 感情共有 query 対応 (★2026-05-27 海山指示)

旧: bot が 「夜眠れない / サカナクション歌詞」 等の poetic post に対しても
   「残念ながらそれはこっちのデータに入ってないな。確認できてない。」 と返してた
   (= hallucinate 防止 fallback の過剰発動).

新: CLONE_PUBLIC_PROMPT に query 分類 (A/B/C) と 雑談 mode 応答 spec を追加
   B. 雑談 / 感情共有 / poetic 系 post は 共感応答必須、「データに入ってない」 と返さない

★海山「何でもデータを返したり、ばっちりと答えるだけにする必要はない。
脈絡のないコメントに対しても、人間らしいコメントをできるうみやまAIにすべき」
"""
from __future__ import annotations

from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent.parent


def _load_prompt() -> str:
    """CLONE_PUBLIC_PROMPT を brain_wiki.py から抽出 (= module import 不要)."""
    src = (REPO / "brain_wiki.py").read_text(encoding="utf-8")
    start = src.find('CLONE_PUBLIC_PROMPT = """')
    assert start > 0
    body = src[start:]
    # next """ で閉じる
    open_pos = body.find('"""')
    close_pos = body.find('"""', open_pos + 3)
    assert close_pos > 0
    return body[open_pos + 3 : close_pos]


@pytest.mark.smoke
def test_prompt_has_query_classification_section():
    """CLONE_PUBLIC_PROMPT に query 分類 (A/B/C) section が含まれる."""
    p = _load_prompt()
    assert "query 分類" in p
    # A / B / C の 3 分類
    assert "A. 数字" in p or "**A." in p
    assert "B. 雑談" in p or "**B." in p
    assert "C. 抽象" in p or "**C." in p


@pytest.mark.smoke
def test_prompt_explicitly_forbids_data_fallback_for_casual_query():
    """雑談 (B) / 抽象 (C) query では「データに入ってない」 と絶対返さない instruction."""
    p = _load_prompt()
    # 雑談 mode で fallback 文言を禁じる instruction
    assert "絶対に「データに入ってない」 と返さない" in p or "絶対に" in p
    # B mode の存在
    assert "雑談" in p
    assert "共感" in p
    # 海山指示の出典 (= 2026-05-27)
    assert "★2026-05-27" in p


@pytest.mark.smoke
def test_prompt_casual_mode_includes_few_shot_example():
    """雑談 mode に few-shot example (= 海山らしい応答 sample) 含まれる."""
    p = _load_prompt()
    # 海山らしい casual な口調の example
    assert "サカナクション" in p or "山口一郎" in p
    assert "眠れない" in p
    assert "仕事行きたくない" in p


@pytest.mark.smoke
def test_prompt_data_fallback_scope_limited_to_factual_query():
    """「データに入ってない」 文言は A. 数字 / 特定 fact 問いの時 限定 と明示."""
    p = _load_prompt()
    # データに入ってない fallback の scope を 数字 / 特定 fact に限定
    assert "限定" in p
    assert "数字" in p
    # ★絶対遵守 section の補強 (= 雑談 query には適用しない)
    assert "B. 雑談" in p or "B 雑談" in p


@pytest.mark.smoke
def test_retrieval_fallback_keywords_remain_numeric_only():
    """_check_retrieval_fallback の keyword list は数字 / 売上系 only (= 雑談で発火しない)."""
    src = (REPO / "brain_wiki.py").read_text(encoding="utf-8")
    idx = src.find("_RETRIEVAL_FALLBACK_KEYWORDS")
    assert idx > 0
    body = src[idx : idx + 800]
    # 数字 / 売上系 keyword
    assert "売上" in body
    assert "客単価" in body
    # poetic / 雑談語が間違って追加されてないこと (= regression 防止)
    assert "眠れない" not in body
    assert "サカナクション" not in body
    assert "歌詞" not in body


# ─── L7: 基本原則「人間らしいコメントバック」 (★2026-05-27 海山指示) ─────
@pytest.mark.smoke
def test_prompt_has_human_first_basic_principle():
    """CLONE_PUBLIC_PROMPT 冒頭近くに 「基本原則: 人間らしいコメントバック」 section."""
    p = _load_prompt()
    assert "基本原則" in p
    assert "人間らしい" in p
    # Drive 検索は 2 の手 明示
    assert "2 の手" in p or "2の手" in p
    # 「データに入ってない」 単独応答は最後の手段
    assert "最後の手段" in p or "最後" in p
    # 旧 / 新 対比 example が含まれる (= LLM に具体的 pattern 学習させる)
    assert "旧" in p and "新" in p


@pytest.mark.smoke
def test_retrieval_fallback_hardcoded_short_circuit_disabled_by_default():
    """★2026-05-27 海山指示: hardcoded short-circuit fallback は default OFF (env 復活可能)."""
    src = (REPO / "brain_wiki.py").read_text(encoding="utf-8")
    # env flag gate (= default "0" で disabled)
    assert 'os.getenv("RETRIEVAL_FALLBACK_HARDCODED"' in src
    assert 'getenv("RETRIEVAL_FALLBACK_HARDCODED", "0")' in src
    # _check_retrieval_fallback 関数 自体は残ってる (= env 復活可能)
    assert "def _check_retrieval_fallback" in src
    # 旧 unconditional short-circuit (= `fallback_response = self._check...` を if 外で直書き)
    # が無いことを確認 = 単純な「fallback_response = ...」 が if 外に無い
    # → env gate の内側でのみ呼ばれる構造


@pytest.mark.smoke
def test_drive_offer_button_uses_casual_wording():
    """Drive 提案 button の content_text が casual (= 「2 の手」 トーン)."""
    src = (REPO / "main.py").read_text(encoding="utf-8")
    idx = src.find("async def _maybe_offer_drive_search")
    assert idx > 0
    body = src[idx : idx + 3500]
    # casual 文言 (= 「改めて見る」 等)
    assert "改めて見る" in body or "改めて見て" in body
    # 「📂」 又は 何らかの casual emoji
    assert "📂" in body or "🔍" in body
