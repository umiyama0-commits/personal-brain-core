"""reflux バッチ承認 (★2026-07-05 海山指示「一括承認できるように」) のテスト。

per-item 捏造ゲート維持 + バッチ内の類似重複保留 + all/prefix UX を検証。
"""
import json
import pathlib
import sys

import pytest

REPO = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))
sys.path.insert(0, str(REPO / "scripts"))

import reflux  # noqa: E402


@pytest.fixture()
def rf(tmp_path, monkeypatch):
    wiki = tmp_path / "wiki"
    (wiki / "knowledge").mkdir(parents=True)
    (wiki / "knowledge" / "src.md").write_text(
        "# src\n情報共有は透明性を高める、が出所の本文。", encoding="utf-8")
    queue = tmp_path / "reflux_queue.jsonl"
    recs = [
        {"id": "rfx-aaaa000001", "status": "pending", "principle": "情報共有は関係者間での透明性を高め、最新の進捗を確認できるようにする。",
         "evidence_file": "knowledge/src.md", "evidence_quote": "情報共有は透明性を高める", "source_domain": "owndays", "type": "judgment"},
        # ↑の言い換え違い (類似 ≥0.8 想定) — バッチでは保留されるべき
        {"id": "rfx-bbbb000002", "status": "pending", "principle": "情報共有は関係者間の透明性を高め、最新の進捗を確認できるようにすることが重要である。",
         "evidence_file": "knowledge/src.md", "evidence_quote": "情報共有は透明性を高める", "source_domain": "owndays", "type": "judgment"},
        # evidence が出所に無い (捏造疑い) — skip されるべき
        {"id": "rfx-cccc000003", "status": "pending", "principle": "全く別の判断軸。品質はコストに優先する。",
         "evidence_file": "knowledge/src.md", "evidence_quote": "この引用は出所に存在しない", "source_domain": "owndays", "type": "judgment"},
    ]
    queue.write_text("\n".join(json.dumps(r, ensure_ascii=False) for r in recs) + "\n", encoding="utf-8")
    monkeypatch.setattr(reflux, "WIKI_DIR", wiki)
    monkeypatch.setattr(reflux, "QUEUE", queue)
    monkeypatch.setattr(reflux, "CORE_TARGET", wiki / "judgment" / "reflux-distilled.md")
    return reflux


@pytest.mark.smoke
def test_ok_all_batch(rf):
    out = rf.handle_command("ok all")
    assert "還流: 1 件" in out            # 1件目のみ適用
    assert "類似重複につき保留" in out      # 2件目は言い換え重複 → pending 維持
    assert "evidence 未検証 skip" in out    # 3件目は捏造ゲートで skip
    pend = {r["id"] for r in rf.list_pending()}
    assert pend == {"rfx-bbbb000002", "rfx-cccc000003"}
    assert "rfx-aaaa000001" in rf.CORE_TARGET.read_text(encoding="utf-8")


@pytest.mark.smoke
def test_prefix_and_multi_ids(rf):
    out = rf.handle_command("ng bbbb cccc")     # prefix (rfx- 省略) 複数
    assert "却下: 2 件" in out
    assert len(rf.list_pending()) == 1


@pytest.mark.smoke
def test_ng_all_requires_confirmation(rf):
    out = rf.handle_command("ng all")
    assert "ng all!" in out and len(rf.list_pending()) == 3   # 実行されない
    out = rf.handle_command("ng all!")
    assert "一括却下: 3 件" in out and rf.list_pending() == []


@pytest.mark.smoke
def test_mixed_all_and_id_rejected(rf):
    assert "同時指定" in rf.handle_command("ok all rfx-aaaa000001")
