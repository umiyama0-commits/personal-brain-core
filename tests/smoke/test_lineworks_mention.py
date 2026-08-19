"""smoke test: lineworks_bot.is_mentioned (★2026-05-24 Tier 0 group silent listen 判定)

LINE WORKS group 内で bot が **mention された時のみ反応** する判定 helper。
plain text "@<bot_name>" を最低限 detect、env override 可。
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(REPO))


@pytest.mark.smoke
def test_mentioned_default_names():
    """default mention name set で plain text mention 検出。"""
    from lineworks_bot import is_mentioned
    assert is_mentioned("@うみやまAI 売上どう?") is True
    assert is_mentioned("おはよう @うみやまAI") is True
    assert is_mentioned("@うみやま 進捗") is True
    assert is_mentioned("@umiyamaAI hello") is True
    assert is_mentioned("@umiyama_ai test") is True


@pytest.mark.smoke
def test_not_mentioned():
    """mention 無しは False。"""
    from lineworks_bot import is_mentioned
    assert is_mentioned("売上どう?") is False
    assert is_mentioned("ありがとう") is False
    assert is_mentioned("") is False
    assert is_mentioned(None) is False


@pytest.mark.smoke
def test_not_mentioned_similar_but_different():
    """類似だが mention じゃない pattern は False。"""
    from lineworks_bot import is_mentioned
    # 「@うみやまさん」 → 「@うみやま」 で hit してしまうので OK 判定だが、
    # 「umiyamaAItest」 (= 区切り無し) は hit しない
    assert is_mentioned("@umiyamaAItest") is False  # AI の直後 to 't' は word boundary 違反
    # ただし「@うみやま」 prefix で「@うみやまさん」は hit する (= 日本語境界判定の限界)
    # この trade-off は許容、誤検出側は LLM が解釈可能なので silent listen 漏れだけ防げれば良い


@pytest.mark.smoke
def test_mentioned_with_punctuation():
    """mention 直後に句読点があっても検出。"""
    from lineworks_bot import is_mentioned
    assert is_mentioned("@うみやまAI、売上どう?") is True
    assert is_mentioned("@うみやまAI! 質問") is True
    assert is_mentioned("@umiyamaAI, please help") is True


@pytest.mark.smoke
def test_mentioned_case_insensitive():
    """case-insensitive 判定。"""
    from lineworks_bot import is_mentioned
    assert is_mentioned("@UmiyamaAI hello") is True
    assert is_mentioned("@UMIYAMA_AI test") is True


@pytest.mark.smoke
def test_mentioned_env_override(monkeypatch):
    """LW_BOT_MENTION_NAMES env で override 可能。"""
    from lineworks_bot import is_mentioned
    monkeypatch.setenv("LW_BOT_MENTION_NAMES", "海山bot,custombot")
    # default 名で検出されなくなる
    assert is_mentioned("@うみやまAI test") is False
    # override 名で検出
    assert is_mentioned("@海山bot test") is True
    assert is_mentioned("@custombot hello") is True


@pytest.mark.smoke
def test_mentioned_no_at_sign():
    """@ 無しの「うみやまAI」言及は mention じゃない (= 単なる話題)。"""
    from lineworks_bot import is_mentioned
    assert is_mentioned("うみやまAI さっき試した") is False
    assert is_mentioned("umiyamaAI is good") is False


# ─── LINE WORKS 公式 <m userId="..."> tag 形式 (★fact-checker verify 済 2026-05-24) ───


@pytest.mark.smoke
def test_mentioned_m_tag_bot_user_id(monkeypatch):
    """<m userId="LW_BOT_USER_ID"> tag が含まれれば mention 成立。"""
    from lineworks_bot import is_mentioned
    monkeypatch.setenv("LW_BOT_USER_ID", "bot_xyz_001")
    assert is_mentioned('<m userId="bot_xyz_001">うみやまAI</m> 売上どう?') is True
    # 大文字小文字差は許容 (case-insensitive)
    assert is_mentioned('<m userId="BOT_XYZ_001">うみやまAI</m> hello') is True
    # シングルクォートも対応
    assert is_mentioned("<m userId='bot_xyz_001'>うみやまAI</m>") is True


@pytest.mark.smoke
def test_not_mentioned_m_tag_other_user(monkeypatch):
    """他 user 向け <m userId="other"> tag だけだと mention 不成立。"""
    from lineworks_bot import is_mentioned
    monkeypatch.setenv("LW_BOT_USER_ID", "bot_xyz_001")
    assert is_mentioned('<m userId="user_other">田中</m> 確認お願い') is False
    # ただし bot 名 plain text fallback で hit する場合は True
    assert is_mentioned('<m userId="user_other">田中</m> @うみやまAI 助けて') is True


@pytest.mark.smoke
def test_mentioned_m_tag_all(monkeypatch):
    """<m userId="all"> (= @All) は bot も含まれる扱いで True。"""
    from lineworks_bot import is_mentioned
    monkeypatch.setenv("LW_BOT_USER_ID", "bot_xyz_001")
    assert is_mentioned('<m userId="all">All</m> 全員見て') is True
    assert is_mentioned('<m userId="ALL">@all</m> ping') is True


@pytest.mark.smoke
def test_m_tag_without_bot_user_id_env(monkeypatch):
    """LW_BOT_USER_ID 未設定なら <m userId="..."> tag は (B)(C) plain text fallback でしか hit しない。"""
    from lineworks_bot import is_mentioned
    monkeypatch.delenv("LW_BOT_USER_ID", raising=False)
    # bot user_id 未設定 → "all" は依然 True (= bot 含まれる前提)
    assert is_mentioned('<m userId="all">All</m> 確認') is True
    # 他 user id への mention は False (LW_BOT_USER_ID 無いので bot 特定不可)
    assert is_mentioned('<m userId="some_user">田中</m> 確認') is False
    # plain text fallback は機能する
    assert is_mentioned('<m userId="some_user">田中</m> @うみやまAI 助けて') is True


@pytest.mark.smoke
def test_strip_mention_tags():
    """strip_mention_tags で <m> tag が「@表示名」に置換される。"""
    from lineworks_bot import strip_mention_tags
    raw = '<m userId="bot_xyz_001">うみやまAI</m> 売上どう?'
    cleaned = strip_mention_tags(raw)
    assert cleaned == "@うみやまAI 売上どう?"

    # 複数 mention
    raw2 = '<m userId="u1">田中</m> と <m userId="u2">鈴木</m> ヘルプ'
    cleaned2 = strip_mention_tags(raw2)
    assert cleaned2 == "@田中 と @鈴木 ヘルプ"

    # tag 無しは変更なし
    assert strip_mention_tags("plain text @うみやまAI") == "plain text @うみやまAI"
    assert strip_mention_tags("") == ""


@pytest.mark.smoke
def test_strip_preserves_text_outside_tags():
    """tag 外の text は完全保持。"""
    from lineworks_bot import strip_mention_tags
    raw = '先頭 <m userId="u1">表示名</m> 中間 <m userId="u2">他人</m> 末尾'
    cleaned = strip_mention_tags(raw)
    assert cleaned == "先頭 @表示名 中間 @他人 末尾"


@pytest.mark.smoke
def test_mention_email_user_id(monkeypatch):
    """userId に email 形式 (公式仕様で email 受付) でも検出。"""
    from lineworks_bot import is_mentioned
    monkeypatch.setenv("LW_BOT_USER_ID", "bot-account@example.co.jp")
    assert is_mentioned('<m userId="bot-account@example.co.jp">うみやまAI</m> hello') is True
    assert is_mentioned('<m userId="other@owndays">他人</m> mention') is False
