"""tests/smoke/test_py39_host_compat.py — host cron の Python 3.9 互換 pin (★2026-07-12).

実障害: magazine_backfill cron の python は system 3.9 (cron 最小 PATH)。7/11 [1f0f4e2] が
stapa_scraper.py に `Path | None` (PEP604 = 3.10+) を追加 → import 即死 → 23 回連続 loud-fail。
本 test は「host で python3 直実行される entry script が PEP604 union を annotation に使うなら、
`from __future__ import annotations` (lazy 化 = 3.9 安全) を必ず持つ」を機械的に固定する。

検出は AST: 関数 def の引数/戻り値 annotation に BinOp(BitOr) があるか。
(§1.8 の dry-run 規律の静的版 — 追記時に踏む前に CI で止める)
"""
from __future__ import annotations

import ast
from pathlib import Path

import pytest

_ROOT = Path(__file__).resolve().parents[2]

# host cron / wrapper が python3 (system 3.9 になり得る) で直接実行する entry script 群。
# 新しい host 実行 script を足したらここにも足す (cron_install.sh 追加時)。
HOST_ENTRY_SCRIPTS = [
    "stapa_scraper.py",
    "apple_notes_sync.py",
    "gdrive_sync.py",
    "mobile_owndays_scraper.py",
    "mobile_owndays_historical.py",
    "kpi_dash_scraper.py",
    "lineworks_scraper.py",
    "scripts/magazine_backfill.py",
    "scripts/magazine_persona_ingest.py",
    "scripts/regulations_sync.py",
    "scripts/receipt_harvester.py",
    "scripts/meeting_autojoin.py",
    "scripts/bot_uptime_monitor.py",
    "scripts/bot_events.py",
    "scripts/bot_metrics.py",
    "scripts/import_inbox_sweep.py",
]


def _has_pep604_annotation(tree: ast.AST) -> bool:
    """def の annotation (引数/return) に X | Y (BitOr) を使っているか。"""
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            anns = [a.annotation for a in node.args.args + node.args.kwonlyargs
                    if a.annotation is not None]
            if node.returns is not None:
                anns.append(node.returns)
            for ann in anns:
                for sub in ast.walk(ann):
                    if isinstance(sub, ast.BinOp) and isinstance(sub.op, ast.BitOr):
                        return True
        # 変数 annotation (module/class レベル) も 3.9 では即評価
        if isinstance(node, ast.AnnAssign) and node.annotation is not None:
            for sub in ast.walk(node.annotation):
                if isinstance(sub, ast.BinOp) and isinstance(sub.op, ast.BitOr):
                    return True
    return False


def _has_future_annotations(src: str) -> bool:
    return "from __future__ import annotations" in src


@pytest.mark.parametrize("rel", HOST_ENTRY_SCRIPTS)
def test_host_script_py39_safe(rel):
    p = _ROOT / rel
    if not p.exists():
        pytest.skip(f"{rel} 不在")
    src = p.read_text(encoding="utf-8")
    tree = ast.parse(src)
    if _has_pep604_annotation(tree):
        assert _has_future_annotations(src), (
            f"{rel}: PEP604 union (X | None) を annotation に使うのに "
            f"`from __future__ import annotations` が無い — host の system python 3.9 で "
            f"import 即死する (2026-07-12 magazine_backfill 23連敗の再発型)")


def test_stapa_scraper_regression():
    """実障害ファイルの直接 pin。"""
    src = (_ROOT / "stapa_scraper.py").read_text(encoding="utf-8")
    assert "from __future__ import annotations" in src
