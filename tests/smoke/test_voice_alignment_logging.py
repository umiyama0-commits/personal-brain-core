"""smoke test: voice_alignment 経路の bot_events 構造化ログ追加 (★2026-05-23 海山指示)

5/21 以降の Vapi transcript 欠落事案の追跡用に、main.py の voice_alignment_webhook +
_process_voice_alignment に log_bot_event を埋め込んだ事を CI で常時検証。
"""
from __future__ import annotations

from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent.parent


@pytest.mark.smoke
def test_voice_alignment_webhook_logs_auth_failed():
    """webhook 認証失敗時に log_bot_event(voice_alignment, auth_failed) を呼ぶ。"""
    src = (REPO / "main.py").read_text(encoding="utf-8")
    # ★2026-06-11 堅牢化: メッセージ非依存。webhook 関数本体に auth_failed 計装があるか構造検証
    # (拒否文言は海山が随時変えるため文字列一致依存をやめる。これが CI 赤の真因だった)
    fn_start = src.find("async def voice_alignment_webhook")
    assert fn_start > 0
    fn_end = src.find("\nasync def ", fn_start + 1)
    fn_body = src[fn_start:fn_end] if fn_end > fn_start else src[fn_start:fn_start + 4000]
    assert "log_bot_event" in fn_body, "log_bot_event 計装が webhook 関数に無い"
    assert "auth_failed" in fn_body, "auth_failed 計装が webhook 関数に無い"


@pytest.mark.smoke
def test_voice_alignment_webhook_logs_webhook_received():
    """webhook 認証通過後 webhook_received event を記録。"""
    # ★2026-06-10 堅牢化: メッセージ非依存。webhook 関数内に webhook_received 計装があるか構造検証。
    src = (REPO / "main.py").read_text(encoding="utf-8")
    fn_start = src.find("async def voice_alignment_webhook")
    assert fn_start > 0
    fn_end = src.find("\nasync def ", fn_start + 1)
    fn_body = src[fn_start:fn_end] if fn_end > fn_start else src[fn_start:fn_start + 4000]
    assert "webhook_received" in fn_body, "webhook_received 計装が webhook 関数に無い"


@pytest.mark.smoke
def test_process_voice_alignment_logs_raw_recorded():
    """raw 保存 (★2026-07-04 webhook 内へ同期化) 後に raw_recorded、蒸留側に
    extracted/extract_failed/process_failed の計装がある。"""
    src = (REPO / "main.py").read_text(encoding="utf-8")
    # raw_recorded は record_session の移設 (同期保存) に伴い webhook 関数側へ
    wh_start = src.find("async def voice_alignment_webhook")
    assert wh_start > 0
    wh_end = src.find("\nasync def ", wh_start + 1)
    wh_body = src[wh_start:wh_end] if wh_end > wh_start else src[wh_start:wh_start + 8000]
    assert "raw_recorded" in wh_body, "raw_recorded 計装が webhook (同期保存) に無い"
    # 蒸留側 (_process_voice_alignment) の計装
    fn_start = src.find("async def _process_voice_alignment")
    assert fn_start > 0
    fn_end = src.find("\n\n\n", fn_start)  # 次の関数まで
    fn_body = src[fn_start:fn_end] if fn_end > 0 else src[fn_start:fn_start + 3000]
    assert "extracted" in fn_body
    assert "extract_failed" in fn_body
    # 上位例外時の process_failed も記録
    assert "process_failed" in fn_body


@pytest.mark.smoke
def test_log_bot_event_failures_swallowed_silently():
    """log_bot_event 自体の例外は本流 (= record/extract) を止めない (= silent except)。"""
    src = (REPO / "main.py").read_text(encoding="utf-8")
    fn_start = src.find("async def _process_voice_alignment")
    fn_end = src.find("\n\n\n", fn_start)
    fn_body = src[fn_start:fn_end] if fn_end > 0 else src[fn_start:fn_start + 3000]
    # try/except Exception: pass で log 失敗を握り潰す
    # (Layer 3 の silent except パターン、本流に伝播させない)
    assert fn_body.count("except Exception:") >= 3
    assert fn_body.count("pass") >= 3
