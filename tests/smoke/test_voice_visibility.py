"""smoke: ブラウザ配送 (Vapi Web SDK) への深層人格 露出制御 (★2026-08-03)。

背景: `/api/voice-alignment/web-config` の戻り値は `vapi.start(cfg)` の仕様上 **ブラウザに平文
JSON で返る**。ゲートは `?token=` の URL クエリのみで、履歴 / access log / Cloudflare log /
LINE 転送に残る。実測で interview 18 file の末尾 = 9,235 字 (家族・弱さ・金・体・内的独白) が
そのまま出ていた。電話経路 (server→Vapi) はブラウザを通らないので全深度を維持する。

固定する不変条件:
1. **allowlist であること** (denylist にすると interview/ に新 file が増えるたび黙って漏れる。
   初版は denylist で書き、§1.15 Reviewer が chronicle.md の素通りを実証した)
2. 未知 stem は既定で遮断 (fail-safe)
3. 電話経路は素通し (機能を殺さない)
4. session 要約はブラウザ配送時に落ちる (次元フィルタが効かないため)
"""
import pathlib
import sys

ROOT = pathlib.Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from brain_wiki_helpers.voice_visibility import (  # noqa: E402
    BROWSER_SAFE_DIMS, interview_files_for_voice, redact_summaries_for_browser,
)

# 過去に素通りが実証された / 明らかに深層な stem。ここに 1 つでも通ると privacy 事故。
MUST_BLOCK = [
    "chronicle",       # 自伝の一次資料 (家系・貧困・家族の病) — 初版 denylist で素通りしていた
    "family", "shadow", "money-personal", "body-health", "inner-voice", "episodes",
    "humor", "taste-daily",
    "future-unknown-deep-dimension",   # 未知 stem = 既定で遮断されること
]


def _mk(tmp_path, stems):
    d = tmp_path / "interview"
    d.mkdir(parents=True)
    for s in stems:
        (d / f"{s}.md").write_text(f"# {s}\n本文\n", encoding="utf-8")
    return d


def test_browser_blocks_all_deep_stems(tmp_path):
    idir = _mk(tmp_path, MUST_BLOCK + sorted(BROWSER_SAFE_DIMS))
    got = {f.stem for f in interview_files_for_voice(idir, browser_delivered=True)}
    leaked = sorted(set(MUST_BLOCK) & got)
    assert not leaked, f"ブラウザ配送に深層 interview が漏れる: {leaked}"


def test_phone_keeps_everything(tmp_path):
    stems = MUST_BLOCK + sorted(BROWSER_SAFE_DIMS)
    idir = _mk(tmp_path, stems)
    got = {f.stem for f in interview_files_for_voice(idir, browser_delivered=False)}
    assert got == set(stems), "電話経路は全深度を維持すること (絞ると人格アラインメントが劣化)"


def test_unknown_stem_defaults_to_blocked(tmp_path):
    """allowlist であることの本質 — 新しい深層 file が増えても自動で遮断される。"""
    idir = _mk(tmp_path, ["totally-new-private-topic"])
    assert interview_files_for_voice(idir, browser_delivered=True) == []


def test_missing_dir_is_safe(tmp_path):
    assert interview_files_for_voice(tmp_path / "nope", browser_delivered=True) == []
    assert interview_files_for_voice(None, browser_delivered=False) == []


def test_session_summaries_dropped_for_browser():
    s = ["家族との距離感について話した", "体調の話"]
    assert redact_summaries_for_browser(s, browser_delivered=True) == []
    assert redact_summaries_for_browser(s, browser_delivered=False) == s


def test_source_level_web_config_passes_browser_delivered():
    """web-config endpoint が browser_delivered=True を渡し続けること (外すと全開に戻る)。"""
    src = (ROOT / "main.py").read_text(encoding="utf-8")
    i = src.find("async def voice_align_web_config")
    assert i > 0, "web-config endpoint が消えた"
    body = src[i:i + 2000]
    assert "browser_delivered=True" in body, \
        "web-config が browser_delivered=True を渡していない (深層人格がブラウザ平文に戻る)"
    # 電話経路 (assistant-request) は逆に渡してはいけない
    j = src.find("_build_voice_align_assistant_config(", src.find("assistant-request"))
    assert j > 0
    assert "browser_delivered=True" not in src[j:j + 400], \
        "電話経路にまで browser_delivered=True が付いた (音声アラインメントの深度が落ちる)"
