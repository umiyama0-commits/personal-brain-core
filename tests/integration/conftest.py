"""
Integration test 用 fixture & mock。

設計:
- 本物の LLM / Chroma / LINE Works を呼ばない (cost & 環境依存ゼロ)
- BrainWiki / BrainIndex / lineworks_bot 等を mock or stub する
- bot 応答 1 turn の full flow (retrieval → memory → LLM → response → logging) を test
"""
from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(REPO_ROOT))


# ─── Mock LLM (litellm 互換 response) ────────────────
class MockHTTPClient:
    """litellm proxy への httpx.AsyncClient.post を mock。

    使い方:
      http = MockHTTPClient(default_response="海山風の応答テキスト")
      response = await http.post(...)  # → MagicMock with .json() → {choices: [{message: {content: ...}}]}
    """

    def __init__(self, default_response: str = "OK 了解", responses_queue: list[str] | None = None):
        self.default = default_response
        self.queue = responses_queue or []
        self.call_count = 0
        self.last_payload: dict | None = None

    async def post(self, url: str, **kwargs):
        self.call_count += 1
        self.last_payload = kwargs.get("json")

        # キューから取るか default
        if self.queue:
            content = self.queue[(self.call_count - 1) % len(self.queue)]
        else:
            content = self.default

        # litellm レスポンス形式 (OpenAI compatible)
        response_data = {
            "choices": [{"message": {"content": content}}],
            "model": "mock-model",
            "usage": {"prompt_tokens": 100, "completion_tokens": 50, "total_tokens": 150},
        }

        mock_resp = MagicMock()
        mock_resp.json = MagicMock(return_value=response_data)
        mock_resp.status_code = 200
        mock_resp.raise_for_status = MagicMock()
        return mock_resp

    async def aclose(self):
        pass


# ─── Mock Chroma index ───────────────────────────────
class MockBrainIndex:
    """BrainIndex.search を mock。

    使い方:
      idx = MockBrainIndex(seed_hits={
          "売上": [{"source": "knowledge/owndays-daily-sales.md",
                   "content": "全体 20M..."}],
      })
      hits = await idx.search("武蔵小山の売上", n_results=15)
    """

    def __init__(self, seed_hits: dict[str, list[dict]] | None = None):
        self.seed_hits = seed_hits or {}
        self.search_calls: list[dict] = []

    async def search(self, query: str, n_results: int = 10,
                     collection: str = "wiki", **kwargs) -> list[dict]:
        self.search_calls.append({"query": query, "n_results": n_results,
                                  "collection": collection})
        # query にマッチする seed を返す
        for kw, hits in self.seed_hits.items():
            if kw in query:
                return hits[:n_results]
        return []


# ─── Brain-isolated fixture ──────────────────────────
@pytest.fixture
def isolated_brain_root(tmp_path, monkeypatch):
    """data/brain 配下を tmp に隔離。BRAIN_ROOT / BRAIN_APP_ROOT 上書き。"""
    data_brain = tmp_path / "data" / "brain"
    for sub in [
        "wiki", "wiki/style", "wiki/knowledge", "wiki/judgment",
        "wiki/reflex", "wiki/embodiment",
        "raw", "raw/conversations", "raw/notes",
        "clone_history", "clone_memory", "clone_improve",
        "bot_events", "eval/external",
        "extractor_state",
    ]:
        (data_brain / sub).mkdir(parents=True, exist_ok=True)

    monkeypatch.setenv("BRAIN_ROOT", str(data_brain))
    monkeypatch.setenv("BRAIN_APP_ROOT", str(tmp_path))
    monkeypatch.setenv("LITELLM_URL", "http://mock-litellm:4000")
    monkeypatch.setenv("LITELLM_MASTER_KEY", "mock-key")

    # 最小限の core wiki を seed (compact retrieval が困らないように)
    (data_brain / "wiki" / "identity.md").write_text(
        "---\nclone_visibility: public\nexit_visibility: public\n---\n"
        "# Identity\n海山丈司 — OWNDAYS CEO\n", encoding="utf-8"
    )
    (data_brain / "wiki" / "style.md").write_text(
        "---\nclone_visibility: public\nexit_visibility: public\n---\n"
        "# Style\n砕けたトーン、断定弱化、概念語回避\n", encoding="utf-8"
    )
    (data_brain / "wiki" / "thinking.md").write_text(
        "---\nclone_visibility: public\nexit_visibility: public\n---\n"
        "# Thinking\nレバレッジ点を 1 つ、推論を明示、開かれた問いで止まる\n",
        encoding="utf-8"
    )

    return data_brain


@pytest.fixture
def mock_http():
    """Mock litellm HTTP client。"""
    return MockHTTPClient(default_response="了解、関東Aエリアの売上は 1,777,111 円。")


@pytest.fixture
def mock_index():
    """Mock Chroma index。"""
    return MockBrainIndex()


@pytest.fixture
def seeded_index():
    """売上系 query にヒットする mock index。"""
    return MockBrainIndex(seed_hits={
        "売上": [
            {"source": "knowledge/owndays-daily-sales.md",
             "content": "全社売上 20,325,213 円、客数 1,228 人"},
            {"source": "knowledge/owndays-daily-stores.md",
             "content": "武蔵小山パルム 19,727 円 / 1 客"},
        ],
        "店舗": [
            {"source": "knowledge/owndays-store-master.md",
             "content": "304 店、6 AM、27 SV、38 都道府県"},
        ],
    })


# ─── BrainWiki instance fixture (mocked dependencies) ─────────────
@pytest.fixture
async def mock_brain_wiki(isolated_brain_root, mock_http, mock_index):
    """BrainWiki インスタンスを mock dependencies で組み立てて返す。

    注意: brain_wiki.py の import が重い (chromadb / litellm) ので
    実環境で初めて整う。Python 3.10+ 必要。
    Python 3.9 ローカルで動かないケースは pytest.skip。
    """
    try:
        from brain_wiki import BrainWiki  # type: ignore
    except Exception as e:
        pytest.skip(f"brain_wiki import failed (Python 3.9 環境では skip): {e}")

    bw = BrainWiki(mock_http, "http://mock-litellm:4000", "mock-key")
    bw.set_index(mock_index)
    return bw
