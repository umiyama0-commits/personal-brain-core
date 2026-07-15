"""tests/smoke/test_voice_align_page.py — voice-align web ページの契約 pin (★2026-07-13 移設時).

守る不変条件:
- render_page がプレースホルダを完全置換する (残ると SDK init 不能)
- 診断計器 3 点が載っている (mic メーター / transcript 表示 / error stringify)
  = 「web で発話しても反応が無い」問題の切り分け計器。剥がすと再発時に盲目化
- main.py 側の gate (VOICE_ALIGN_TOKEN fail-closed) が render_page 呼び出しより前にある
"""
from __future__ import annotations

from pathlib import Path

from services.voice_align_page import render_page

_ROOT = Path(__file__).resolve().parents[2]


def test_render_page_substitutes_placeholders():
    html = render_page("pk_test123", "/api/voice-alignment/web-config?token=t")
    assert "pk_test123" in html
    assert "/api/voice-alignment/web-config?token=t" in html
    assert "__VAPI_PUBLIC_KEY__" not in html
    assert "__CONFIG_URL__" not in html


def test_diagnostic_instruments_present():
    html = render_page("pk", "/c")
    # ① マイク入力レベルメーター (Vapi と独立の getUserMedia)
    assert "startMicMeter" in html and "getUserMedia" in html
    # ② リアルタイム文字起こし (🎤 行が出ない = STT に届いていない、の切り分け)
    assert '"transcript"' in html and "transcriptType" in html
    # ③ error object の stringify ([object Object] 撲滅)
    assert "errText" in html and "JSON.stringify" in html


def test_main_route_gate_before_render():
    src = (_ROOT / "main.py").read_text(encoding="utf-8")
    route = src[src.index('@app.get("/voice-align", response_class=HTMLResponse)'):][:2000]
    i_gate = route.index("VOICE_ALIGN_TOKEN")
    i_render = route.index("render_page")
    assert i_gate < i_render, "token gate が render_page より前に無い (fail-closed 崩れ)"
    # 移設完了 = 巨大 HTML が main.py に残っていない
    assert "_VOICE_ALIGN_HTML" not in src
