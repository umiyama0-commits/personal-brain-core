"""prompt_version の test (LLMOps G2: prompt を eval の因果に紐付ける)。"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))

from prompt_version import _extract_prompts, prompt_version  # noqa: E402


def test_extract_triple_quoted():
    src = 'X = 1\nCLONE_PROMPT = """body-a\nline2"""\nCLONE_PUBLIC_PROMPT = """body-b"""\n'
    ex = _extract_prompts(src)
    assert "body-a" in ex
    assert "body-b" in ex


def test_extract_empty_on_no_match():
    assert _extract_prompts("nothing here") == ""


def test_version_deterministic():
    assert prompt_version() == prompt_version()


def test_version_is_short_hex():
    v = prompt_version()
    assert len(v) == 12
    int(v, 16)  # hex として valid (例外なら fail)


def test_version_changes_with_content():
    # _extract_prompts の出力が変われば hash 源が変わる前提を確認 (純関数レベル)
    a = _extract_prompts('CLONE_PROMPT = """v1"""')
    b = _extract_prompts('CLONE_PROMPT = """v2"""')
    assert a != b
