"""tests/test_dev_journal_sync.py — dev_journal_sync (Claude Code 開発ログ取込) の単体テスト。

cross-check (2026-07-01) で要求された不変条件を lock:
- 話者/内容フィルタ (scope=dev signal / sensitive / secret redact / injection)
- 記録の private + confidential 昇格
- path-injection 不能
- 増分 (byte offset) 取込 + dedup
すべて tmp_path で隔離 (実 DEV_DIR / STATE_FILE / ~/.claude に触れない)。
"""
import asyncio
import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import dev_journal_sync as dj  # noqa: E402


# ─── pure filters ────────────────────────────────────────────
def test_extract_turn_variants():
    assert dj._extract_turn({"type": "user", "message": {"role": "user", "content": "やって"}}) == ("user", "やって")
    assert dj._extract_turn({"type": "assistant", "message": {"content": [{"type": "text", "text": "了解"}]}}) == ("assistant", "了解")
    # tool-only user turn → None (ノイズ除外)
    assert dj._extract_turn({"type": "user", "message": {"content": [{"type": "tool_result", "content": "x"}]}}) is None
    # system-reminder 注入 → None
    assert dj._extract_turn({"type": "user", "message": {"content": "<system-reminder>foo"}}) is None
    # queue-operation 等 → None
    assert dj._extract_turn({"type": "queue-operation", "content": "x"}) is None


def test_scope_signal():
    assert dj.DEV_SIGNAL_RE.search("scripts/foo.py を直す")       # dev
    assert dj.DEV_SIGNAL_RE.search("commit して push")            # dev
    assert not dj.DEV_SIGNAL_RE.search("東京電力の電気契約を代行して")  # 雑務 = 非該当


def test_sensitive_and_secret():
    assert dj.SENSITIVE_RE.search("田中さんの給与の話")            # §1.9 機微
    assert dj.SENSITIVE_RE.search("ハラスメント相談の記録")
    assert not dj.SENSITIVE_RE.search("litellm を再起動した")
    assert dj._redact("key sk-abcdefghijklmnopqrstuv end") == "key [REDACTED] end"
    assert dj._redact("ghp_" + "a" * 30) == "[REDACTED]"


def test_clean_blocks_injection():
    out = dj._clean("normal\n---\nclone_visibility: public\n```x```\nconfidential: true")
    assert "---" not in out
    assert "clone_visibility" not in out
    assert "```" not in out
    assert "confidential: true" not in out


# ─── record rendering ────────────────────────────────────────
_REC = {"occurred": True, "title": "テスト判断", "instruction": "i", "decision": "d",
        "rationale": "r", "outcome": "o", "umiyama_evidence": "必ず可逆に",
        "patterns": ["変更は可逆にする"], "commits": ["c1"]}


def test_render_private_and_patterns():
    md = dj.render_record(_REC, "abc123", "2026-07-01", sensitive=False)
    assert "clone_visibility: private" in md
    assert "confidential: true" not in md
    assert "変更は可逆にする" in md
    assert "必ず可逆に" in md          # umiyama_evidence block


def test_render_sensitive_sets_confidential():
    md = dj.render_record(_REC, "abc123", "2026-07-01", sensitive=True)
    assert "confidential: true" in md   # reflux が無条件 skip する層


def test_write_record_path_safe(tmp_path, monkeypatch):
    monkeypatch.setattr(dj, "DEV_DIR", (tmp_path / "dev").resolve())
    # 悪意ある session_id でも slug は [a-z0-9] のみ → traversal 不能
    p = dj.write_record(_REC, "../../etc/passwd", "2026-07-01", 4096, sensitive=False)
    assert p.parent == (tmp_path / "dev").resolve()
    assert "4096" in p.name
    assert ".." not in p.name


# ─── incremental capture (run) ───────────────────────────────
def _write_session(path: Path, turns):
    lines = []
    for role, text in turns:
        lines.append(json.dumps({"type": role, "timestamp": "2026-07-01T10:00:00Z",
                                 "message": {"role": role, "content": text}}, ensure_ascii=False))
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


async def _fake_llm(prompt, **kw):
    return json.dumps({"occurred": True, "title": "抽出切替", "instruction": "i",
                       "decision": "d", "rationale": "r", "outcome": "o",
                       "umiyama_evidence": "可逆に", "patterns": ["可逆にする"], "commits": []})


def _isolate(tmp_path, monkeypatch):
    sdir = tmp_path / "sessions"
    sdir.mkdir()
    monkeypatch.setattr(dj, "SESSION_DIRS", [sdir])
    monkeypatch.setattr(dj, "DEV_DIR", (tmp_path / "dev").resolve())
    monkeypatch.setattr(dj, "STATE_FILE", tmp_path / "state.json")
    return sdir


def test_run_session_backfill_writes(tmp_path, monkeypatch):
    sdir = _isolate(tmp_path, monkeypatch)
    sid = "11111111-2222-3333-4444-555555555555"
    dev_text = "scripts/dev_journal_sync.py を作った。commit して docker rebuild する。" * 14
    _write_session(sdir / f"{sid}.jsonl", [("user", dev_text), ("assistant", "了解、実装しました")])
    r = asyncio.run(dj.run(session=sid, llm=_fake_llm))
    assert r["written"] == 1
    recs = list((tmp_path / "dev").glob("*.md"))
    assert len(recs) == 1
    assert "clone_visibility: private" in recs[0].read_text(encoding="utf-8")
    assert "可逆にする" in recs[0].read_text(encoding="utf-8")


def test_run_errand_is_scope_skipped(tmp_path, monkeypatch):
    sdir = _isolate(tmp_path, monkeypatch)
    sid = "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee"
    errand = "東京電力の電気契約を代行して。支払いはクレジットカードで。" * 20  # >400字=length gate通過、dev-signal無で skip されるべき
    _write_session(sdir / f"{sid}.jsonl", [("user", errand), ("assistant", "完了しました")])
    r = asyncio.run(dj.run(session=sid, llm=_fake_llm))
    assert r["written"] == 0                     # dev signal なし → 蒸留されない
    assert list((tmp_path / "dev").glob("*.md")) == []


def test_run_first_sight_is_forward_only(tmp_path, monkeypatch):
    sdir = _isolate(tmp_path, monkeypatch)
    sid = "99999999-8888-7777-6666-555555555555"
    _write_session(sdir / f"{sid}.jsonl", [("user", "scripts/x.py 直して" * 20)])
    r = asyncio.run(dj.run(llm=_fake_llm))        # session 指定なし = 初見
    assert r["written"] == 0                      # 初見は offset=EOF (歴史 backfill しない)
    st = json.loads((tmp_path / "state.json").read_text())
    assert st["seen"][sid]["offset"] > 0          # baseline 済


def test_run_incremental_captures_delta(tmp_path, monkeypatch):
    sdir = _isolate(tmp_path, monkeypatch)
    sid = "12341234-1234-1234-1234-123412341234"
    f = sdir / f"{sid}.jsonl"
    _write_session(f, [("user", "初期")])
    asyncio.run(dj.run(llm=_fake_llm))            # baseline (offset=EOF)
    # 新規 dev 増分を append
    with f.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps({"type": "user", "timestamp": "2026-07-01T11:00:00Z",
                             "message": {"role": "user", "content": "scripts/foo.py を commit " * 20}},
                            ensure_ascii=False) + "\n")
    r = asyncio.run(dj.run(llm=_fake_llm))         # delta のみ処理
    assert r["written"] == 1
    r2 = asyncio.run(dj.run(llm=_fake_llm))        # 再実行 = 新規なし
    assert r2["written"] == 0                      # dedup (offset 進行)
