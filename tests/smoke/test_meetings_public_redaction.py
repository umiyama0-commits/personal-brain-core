"""smoke test: 議事録の公開要約 (機密除外) の安全性質 (★2026-06-01 海山指示)

「うみやまAI に議事録を出すが要約だけ」= 機密 (物件名/競合比較/エリア別数値/人事) を
除外した公開要約のみを全社員クローンの core に載せる。board minutes → 全社員 + 社外流出
リスクを考慮した privacy-critical 機能なので、安全性質を source レベルで回帰防止する。

実 LLM 呼出無し、純粋な source 検証。
"""
from __future__ import annotations

from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent.parent
SRC = (REPO / "brain_wiki.py").read_text(encoding="utf-8")


@pytest.mark.smoke
def test_redaction_has_failsafe():
    """redact 失敗/空/異常長は "(社内限定)" で機密を露出させない (fail-safe)。"""
    i = SRC.find("async def _redact_meeting_for_public")
    assert i > 0, "_redact_meeting_for_public 未定義"
    body = SRC[i:i + 2500]
    # except 経路で社内限定を返す (露出させない)
    assert 'return "(社内限定)"' in body, "redaction の fail-safe が無い"
    # 空 or 異常長も社内限定
    assert "len(out) > 400" in body and "not out" in body, "空/異常長ガードが無い"


@pytest.mark.smoke
def test_redaction_prompt_excludes_sensitive():
    """redaction prompt が機密カテゴリ除外 + 保守ルールを明示。"""
    i = SRC.find("async def _redact_meeting_for_public")
    body = SRC[i:i + 2500]
    assert "物件名" in body, "物件名の除外指示が無い"
    assert "競合" in body, "競合除外指示が無い"
    assert "人事" in body, "人事除外指示が無い"
    assert "迷ったら除外" in body, "保守ルールが無い"


@pytest.mark.smoke
def test_public_index_is_public_and_drops_internal_only():
    """公開 index は clone_visibility: public で、"(社内限定)" を出力から除外する。"""
    i = SRC.find("async def _refresh_meetings_recent_public_index")
    assert i > 0, "_refresh_meetings_recent_public_index 未定義"
    body = SRC[i:i + 4000]
    assert "clone_visibility: public" in body, "公開 index が public でない"
    # 社内限定 は公開 index に載せない
    assert 'pub != "(社内限定)"' in body, "社内限定エントリを除外していない"


@pytest.mark.smoke
def test_public_meetings_in_core_private_not_elevated():
    """meetings-recent-public.md が public core (registry priority 5)、private は据置 3。"""
    # registry: public 版が priority 5、private 版は 3 (公開クローンでは skip されるため据置)
    assert '"knowledge/meetings-recent-public.md": ("meetings", 5)' in SRC
    assert '"knowledge/meetings-recent.md": ("meetings", 3)' in SRC
    # core_files_truncated にも public 版が登録されている
    assert '"knowledge/meetings-recent-public.md",' in SRC


@pytest.mark.smoke
def test_public_refresh_wired_into_compile():
    """compile_meeting_note 経路で public index 生成も呼ばれる。"""
    assert "await self._refresh_meetings_recent_public_index()" in SRC
