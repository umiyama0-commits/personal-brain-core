"""
smoke test 共通 fixtures。

設計:
- 全 test は tmp_path で隔離 (本物の data/brain には絶対触れない)
- 環境変数 BRAIN_APP_ROOT / BRAIN_ROOT を fixture で書き換え
- LLM / HTTP / Chroma は mock または skip

パス計算 (clone_history と clone_improve_lib で異なる):
- clone_history.py: BRAIN_ROOT (default /app/data/brain) → HISTORY_DIR = BRAIN_ROOT / "clone_history"
- clone_improve_lib.py: BRAIN_APP_ROOT (default /app) → DATA_BRAIN = APP_ROOT / "data" / "brain"
                                                      → HISTORY_DIR = DATA_BRAIN / "clone_history"

統一: BRAIN_APP_ROOT = tmp_path、BRAIN_ROOT = tmp_path / "data" / "brain" にして
両者から見える HISTORY_DIR = tmp_path / "data" / "brain" / "clone_history" にする。
"""
from __future__ import annotations

import asyncio
import json
import os
import sys
from pathlib import Path
from datetime import datetime, timezone, timedelta

import pytest

# Repository root を import path に追加
REPO_ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(REPO_ROOT))


@pytest.fixture(autouse=True)
def _asyncio_event_loop_isolation():
    """テスト間の event loop policy 汚染を修復する (★2026-07-05)。

    asyncio.run() は終了時 (finally) に set_event_loop(None) を呼ぶため、
    Python 3.9 では以降の asyncio.get_event_loop() が loop を自動生成せず
    RuntimeError になる。suite 内で asyncio.run() を使う test (13 file) が
    先に走ると、get_event_loop().run_until_complete() を使う test が
    単独 pass / 全体 fail するテスト間干渉が起きていた。

    各 test 終了時に policy が汚染状態 (loop 取得不能 or closed) なら
    default policy へリセットし、次の test を単独実行と同じ状態で始める。
    """
    yield
    try:
        broken = asyncio.get_event_loop_policy().get_event_loop().is_closed()
    except RuntimeError:
        broken = True
    if broken:
        asyncio.set_event_loop_policy(None)


@pytest.fixture
def brain_root(tmp_path, monkeypatch):
    """隔離 BRAIN_ROOT (= tmp_path/data/brain) を作って環境変数を書き換える。

    clone_history.py は BRAIN_ROOT を直接見るので tmp_path/data/brain に揃える。
    clone_improve_lib.py は BRAIN_APP_ROOT を見るので tmp_path に揃える。
    両方から HISTORY_DIR が同じ場所 (tmp_path/data/brain/clone_history) になる。
    """
    data_brain = tmp_path / "data" / "brain"
    data_brain.mkdir(parents=True, exist_ok=True)

    monkeypatch.setenv("BRAIN_ROOT", str(data_brain))
    monkeypatch.setenv("BRAIN_APP_ROOT", str(tmp_path))

    # 主要 sub dirs
    for sub in ["clone_history", "clone_improve", "metrics/daily",
                "alignment/interview_extracted"]:
        (data_brain / sub).mkdir(parents=True, exist_ok=True)

    # alignment/interview_coverage.json は空 JSON
    (data_brain / "alignment" / "interview_coverage.json").write_text("{}", encoding="utf-8")

    yield data_brain


@pytest.fixture
def sample_clone_history(brain_root):
    """clone_history/ に 2 user, 計 6 record をサンプル投入。"""
    base = datetime(2026, 5, 20, 9, 0, tzinfo=timezone(timedelta(hours=9)))
    records_per_user = {
        "user_alice": [
            ("user", "店舗売上どうですか?", base),
            ("assistant", "今日の全社は 20M、客数 1,228 です。", base + timedelta(minutes=1)),
            ("user", "ありがとう", base + timedelta(minutes=2)),
        ],
        "user_bob": [
            ("user", "30 代のキャリアで迷ってます", base + timedelta(hours=1)),
            ("assistant", "30 代でキャリアに迷うのは気持ちわかるよ。", base + timedelta(hours=1, minutes=1)),
            ("user", "違う、もっと具体的なアドバイスが欲しい", base + timedelta(hours=1, minutes=2)),
        ],
    }
    hdir = brain_root / "clone_history"
    for uid, recs in records_per_user.items():
        lines = []
        for role, text, ts in recs:
            lines.append(json.dumps({
                "timestamp": ts.isoformat(),
                "user_id": uid,
                "user_display": uid.replace("user_", "").title(),
                "role": role,
                "text": text,
            }, ensure_ascii=False))
        (hdir / f"{uid}.jsonl").write_text("\n".join(lines) + "\n", encoding="utf-8")
    return brain_root


@pytest.fixture
def sample_alignment_extracted(brain_root):
    """alignment/interview_extracted/ にサンプル蒸留 1 件投入。"""
    edir = brain_root / "alignment" / "interview_extracted"
    fid = "2026-05-20-0044"
    data = {
        "extracted_at": "2026-05-20T20:00:00+09:00",
        "session_summary": "孤独感を好む理由や決断の哲学について話した。",
        "status": "pending_review",
        "items": [
            {
                "category": "philosophy",
                "confidence": "high",
                "wiki_target": "wiki/interview/philosophy.md",
                "insight": "孤独は嫌いじゃない、むしろ判断する時の集中力を生む",
                "evidence_quote": "一人の時間が一番ちゃんと考えられる",
            },
            {
                "category": "value_roots",
                "confidence": "medium",
                "wiki_target": "wiki/interview/value_roots.md",
                "insight": "コントロールできることに集中、できないことは受け入れる",
                "evidence_quote": "外的な要因に振り回されるのは嫌だ",
            },
        ],
    }
    (edir / f"{fid}.json").write_text(
        json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    return fid


class MockLLMClient:
    """LLM 呼び出しを mock するシンプル client。"""

    def __init__(self, responses=None):
        self.responses = responses or ["mock response"]
        self.call_count = 0
        self.last_prompt = None

    async def call(self, prompt: str, **kwargs) -> str:
        self.last_prompt = prompt
        resp = self.responses[self.call_count % len(self.responses)]
        self.call_count += 1
        return resp


@pytest.fixture
def mock_llm():
    return MockLLMClient()
