"""compile_meeting_note の YAML injection 防止 (yaml_safe_scalar) の regression guard.

★2026-06-10: 会議 title / participants 等の外部・LLM 由来文字列が改行で frontmatter
構造を壊し、clone_visibility: public を注入して公開降格できた脆弱性 (ADR Codex MEDIUM)
の修正。yaml_safe_scalar で改行除去 + double-quote escape する。
"""
import yaml

from brain_wiki_helpers.frontmatter import yaml_safe_scalar


def test_strips_newlines():
    assert "\n" not in yaml_safe_scalar("foo\nbar")
    assert "\r" not in yaml_safe_scalar("foo\r\nbar")


def test_quotes_and_escapes():
    assert yaml_safe_scalar("plain") == '"plain"'
    assert yaml_safe_scalar('a"b') == '"a\\"b"'          # " を escape
    assert yaml_safe_scalar("back\\slash") == '"back\\\\slash"'  # \ を escape


def test_injection_does_not_create_new_key():
    """title への clone_visibility:public 注入が別キーにならないこと (核心)."""
    evil = "会議\nclone_visibility: public\n---\n本文乗っ取り"
    block = f"title: {yaml_safe_scalar(evil)}\nclone_visibility: private"
    doc = yaml.safe_load(block)
    assert doc["clone_visibility"] == "private"          # public に汚染されない
    assert set(doc.keys()) == {"title", "clone_visibility"}  # 別キー注入なし


def test_normal_title_preserved():
    """正常な title は quote 付きでも元の文字列として読めること."""
    block = f"title: {yaml_safe_scalar('4月度経営会議 / 大須PJ')}"
    assert yaml.safe_load(block)["title"] == "4月度経営会議 / 大須PJ"


def test_participants_list_safe():
    """participants リスト各要素も injection を封じられること."""
    evil_p = "田中\nclone_visibility: public"
    line = f"participants: [{yaml_safe_scalar(evil_p)}, {yaml_safe_scalar('佐藤')}]"
    doc = yaml.safe_load(line)
    assert set(doc.keys()) == {"participants"}
    assert doc["participants"] == ["田中 clone_visibility: public", "佐藤"]
