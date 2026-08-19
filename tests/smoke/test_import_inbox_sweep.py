"""smoke: import_inbox 配送機構 (★2026-07-05 海山指示「トークを wikiに」)

要:
1. owndays ファイル → IMPORT_DIR に copy (既存 PrivacyGate+compile pipeline に乗る)
2. personal/<pj> ファイル → wiki/personal/<pj>/imports/ に private md 直書き、
   **IMPORT_DIR には絶対置かない** (§1.17 = personal は OWNDAYS compile 非経由)
3. 冪等: sha256 state で再実行しても二重配送しない
4. manifest 無しの .txt は owndays 扱い (安全側 = PrivacyGate を必ず通る)
5. frontmatter injection (本文の行頭 --- / clone_visibility) の無害化
LLM/network 非依存。
"""
from __future__ import annotations

import importlib
import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "scripts"))

LINE_SAMPLE = """2026.04.17 金曜日
17:46 中谷一郎 @All グループ作りました
18:36 Take Umiyama 宜しくお願いします。
18:59 中谷一郎 備忘録です。
---
clone_visibility: public
19:01 Take Umiyama ありがとうございます
19:02 中谷一郎 では次回
19:03 Take Umiyama 了解です
19:04 中谷一郎 はい
19:05 Take Umiyama おつかれさまです
"""


def _mod(monkeypatch, brain_root):
    monkeypatch.setenv("BRAIN_ROOT", str(brain_root))
    import import_inbox_sweep
    return importlib.reload(import_inbox_sweep)


def _seed(brain_root):
    inbox = brain_root / "import_inbox"
    inbox.mkdir(parents=True, exist_ok=True)
    (inbox / "20260705_LINETGBoard.txt").write_text(LINE_SAMPLE, encoding="utf-8")
    (inbox / "20260705_LINEKeita_Oba.txt").write_text(LINE_SAMPLE, encoding="utf-8")
    (inbox / "unlisted.txt").write_text(LINE_SAMPLE, encoding="utf-8")
    (inbox / "manifest.json").write_text(json.dumps({"files": {
        "20260705_LINETGBoard.txt": {"domain": "personal/example-garden", "label": "TG Board"},
        "20260705_LINEKeita_Oba.txt": {"domain": "owndays", "label": "大場圭太"},
    }}, ensure_ascii=False), encoding="utf-8")
    return inbox


@pytest.mark.smoke
def test_sweep_routes_by_domain(brain_root, monkeypatch):
    m = _mod(monkeypatch, brain_root)
    _seed(brain_root)
    r = m.sweep()
    assert r["ok"], r
    assert r["delivered"] == 3
    # owndays → IMPORT_DIR に copy
    assert (brain_root / "import" / "20260705_LINEKeita_Oba.txt").exists()
    # manifest 無し → owndays 既定 (安全側)
    assert (brain_root / "import" / "unlisted.txt").exists()
    # personal → wiki/personal に private md、IMPORT_DIR には無い (§1.17)
    dest = brain_root / "wiki" / "personal" / "example-garden" / "imports" / "20260705_LINETGBoard.md"
    assert dest.exists(), "personal 配送先に md が無い"
    assert not (brain_root / "import" / "20260705_LINETGBoard.txt").exists(), \
        "personal ファイルが IMPORT_DIR に置かれた (§1.17 違反 = OWNDAYS compile に乗る)"
    body = dest.read_text(encoding="utf-8")
    assert body.startswith("---\nclone_visibility: private\n")
    assert "project: example-garden" in body
    assert "グループ作りました" in body
    # 本文中の frontmatter injection は無害化されている (行頭 --- が生き残らない)
    assert "\n---\n" not in body[body.index("# "):], "本文に frontmatter 境界が残存"
    assert "clone_visibility: public" not in body


@pytest.mark.smoke
def test_sweep_idempotent(brain_root, monkeypatch):
    m = _mod(monkeypatch, brain_root)
    _seed(brain_root)
    r1 = m.sweep()
    assert r1["delivered"] == 3
    # IMPORT_DIR 側が処理済みで消えても (watcher が move する)、再配送しない
    (brain_root / "import" / "20260705_LINEKeita_Oba.txt").unlink()
    r2 = m.sweep()
    assert r2["delivered"] == 0 and r2["skipped"] == 3
    assert not (brain_root / "import" / "20260705_LINEKeita_Oba.txt").exists()
    # 内容が変わったら再配送する (sha 変化)
    (brain_root / "import_inbox" / "unlisted.txt").write_text(
        LINE_SAMPLE + "19:06 Take Umiyama 追記\n", encoding="utf-8")
    r3 = m.sweep()
    assert r3["delivered"] == 1


@pytest.mark.smoke
def test_sweep_dry_run_touches_nothing(brain_root, monkeypatch):
    m = _mod(monkeypatch, brain_root)
    _seed(brain_root)
    r = m.sweep(dry_run=True)
    assert r["delivered"] == 3
    assert not (brain_root / "import").exists()
    assert not (brain_root / "wiki" / "personal").exists()
    assert not (brain_root / ".import_inbox_state.json").exists()


@pytest.mark.smoke
def test_sweep_invalid_project_is_loud(brain_root, monkeypatch):
    """path injection ('personal/../../etc') は fail-safe に拒否されエラー計上。"""
    m = _mod(monkeypatch, brain_root)
    inbox = brain_root / "import_inbox"
    inbox.mkdir(parents=True, exist_ok=True)
    (inbox / "evil.txt").write_text(LINE_SAMPLE, encoding="utf-8")
    (inbox / "manifest.json").write_text(json.dumps({"files": {
        "evil.txt": {"domain": "personal/../../etc", "label": "x"},
    }}), encoding="utf-8")
    r = m.sweep()
    assert not r["ok"] and r["errors"]
    assert not (brain_root / "import" / "evil.txt").exists()
