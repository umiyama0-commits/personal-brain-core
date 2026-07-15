"""tests/smoke/test_compile_number_scrub.py — bot 発話数値の wiki 昇格ガード pin (★2026-07-13).

failure-log 2026-07-13: クローンの捏造売上が compile で wiki/decisions/ に confidence: high
昇格した二次汚染の再発防止。守る不変条件:
- bot (うみやまAI) 発話にのみ由来する数値は 〔数値略〕 化 + 注記 (行ごと消さない = 文脈保持)
- 人間発話由来の数値は保持 (海山が言った数字は teach として正当)
- 会話ログでない source (話者ラベル無し) は無変更
- brain_wiki.py compile 経路の配線 (source pin)
"""
from __future__ import annotations

from pathlib import Path

from brain_wiki_helpers.compile_number_scrub import scrub_bot_numbers, split_speaker_text

_ROOT = Path(__file__).resolve().parents[2]

_RAW = """# LINE chat: lineworks_うみやまAI_2026-07-13

## 2026-07-13

[14:53] 海山丈司: 先週の関東エリアの売り上げについて教えて。目標は 100,000,000円 だったはず
[14:53] うみやまAI: 先週の関東A+B 合計は客数 8,000、売上 76,000,000円、客単価 9,500円。
関東Aエリアは客数 5,500。
[14:58] 海山丈司: ありがとう
"""


def test_bot_only_numbers_are_redacted():
    compiled = ("- 2026-07-07〜2026-07-12 の関東A+B 合計売上は、客数 8,000、"
                "売上 76,000,000円、客単価 9,500円だった。\n"
                "- 目標は 100,000,000円 と共有された。")
    out, n = scrub_bot_numbers(compiled, _RAW)
    assert n == 3
    assert "8,000" not in out and "76,000,000" not in out and "9,500" not in out
    assert "〔数値略〕" in out
    assert "100,000,000円" in out          # 海山発話由来は保持
    assert "捏造リスク" in out              # 注記 footer
    # 行は消えていない (定性的文脈の保持)
    assert "関東A+B 合計売上" in out


def test_human_numbers_kept_and_dates_untouched():
    compiled = "### 2026-07-13\n- 目標 100,000,000円 の進捗が確認された。"
    out, n = scrub_bot_numbers(compiled, _RAW)
    assert n == 0 and out == compiled  # 日付 (4桁区切り無し) は token 対象外


def test_non_conversation_source_unchanged():
    raw_plain = "# 会議メモ\n売上は 123,456,789円 でした。"
    compiled = "- 売上は 123,456,789円。"
    out, n = scrub_bot_numbers(compiled, raw_plain)
    assert n == 0 and out == compiled


def test_split_speaker_continuation_lines():
    parts = split_speaker_text(_RAW, ("うみやまAI",))
    assert parts is not None
    human, bot = parts
    assert "100,000,000" in human and "100,000,000" not in bot
    assert "5,500" in bot          # bot 発話の継続行 (改行跨ぎ) も bot 側に帰属
    assert "5,500" not in human


def test_ingest_conversation_markdown_format():
    """★cross-check DA (REAL 高): 常時稼働の会話取込 (brain_wiki.ingest_conversation) は
    '**User**: / **AI**:' 形式。初版は [HH:MM] 形式しか見ておらず本番経路で no-op だった。
    この形式で bot 数値が scrub されることを pin (剥がすと二次汚染ガードが本番で嘘になる)。"""
    raw_md = (
        "## 14:53\n"
        "**User**: 先週の関東エリアの売り上げについて教えて\n"
        "**AI**: 先週の関東A+B 合計は客数 8,000、売上 76,000,000円。\n"
        "内訳は関東A 5,500。\n"
        "## 14:58\n"
        "**User**: 目標は 100,000,000円 だったよね\n"
    )
    compiled = "- 関東A+B 合計は客数 8,000、売上 76,000,000円 (目標 100,000,000円)。"
    out, n = scrub_bot_numbers(compiled, raw_md)
    assert n == 2
    assert "8,000" not in out and "76,000,000" not in out
    assert "100,000,000円" in out  # human 発話由来は保持
    # 継続行 (**AI** の次行) も bot 帰属
    out2, n2 = scrub_bot_numbers("- 関東Aは 5,500 だった。", raw_md)
    assert n2 == 1 and "5,500" not in out2


def test_brain_wiki_compile_wiring():
    src = (_ROOT / "brain_wiki.py").read_text(encoding="utf-8")
    i_scrub = src.index("scrub_bot_numbers")
    i_apply = src.index("self._apply_update(update)")
    assert i_scrub < i_apply, "scrub が _apply_update より前に無い (昇格前ガードが効かない)"
    assert "AI/bot 側の発話にのみ登場する具体的な数値" in src  # COMPILE_PROMPT 指示
