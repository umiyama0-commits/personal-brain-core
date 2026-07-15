"""extractor テスト用 fixtures。

各テストは tmp_path 配下の隔離された "Brain" を使う:
- BRAIN_APP_ROOT を tmp_path に上書き
- _common モジュールを reload して定数を再評価
- raw/wiki/audit/meta/extractor_state ディレクトリを構築

これで本物の data/brain/ には一切触らずに extractor の単体テストが書ける。
"""
from __future__ import annotations

import importlib
import os
import sys
from pathlib import Path

import pytest

# scripts/extractors を sys.path に追加
REPO_ROOT = Path(__file__).resolve().parents[2]
EXTRACTOR_DIR = REPO_ROOT / "scripts" / "extractors"
if str(EXTRACTOR_DIR) not in sys.path:
    sys.path.insert(0, str(EXTRACTOR_DIR))


@pytest.fixture
def brain_root(tmp_path, monkeypatch):
    """tmp_path に隔離された data/brain/ を作り、BRAIN_APP_ROOT を上書きする。

    返り値: Path (= tmp_path = APP_ROOT 相当)
    各 extractor module を再 import すれば、定数 (RAW_DIR / WIKI_DIR 等) が
    この path 配下を指すようになる。
    """
    app_root = tmp_path
    data_brain = app_root / "data" / "brain"
    for sub in (
        "raw/conversations",
        "raw/notes",
        "wiki/style",
        "wiki/judgment",
        "wiki/reflex",
        "wiki/embodiment",
        "wiki/decisions",
        "audit/resolved",
        "meta",
        "extractor_state",
        "schema",
    ):
        (data_brain / sub).mkdir(parents=True, exist_ok=True)

    monkeypatch.setenv("BRAIN_APP_ROOT", str(app_root))

    # _common を再 import して定数を再評価
    if "_common" in sys.modules:
        importlib.reload(sys.modules["_common"])
    else:
        import _common  # noqa: F401

    return app_root


@pytest.fixture
def common(brain_root):
    """_common モジュール (brain_root が再評価された状態) を返す。"""
    import _common
    importlib.reload(_common)
    return _common
