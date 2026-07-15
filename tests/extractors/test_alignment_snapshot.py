"""alignment_snapshot の単体テスト。"""
from __future__ import annotations

import asyncio
import importlib
import sys
from datetime import date
from pathlib import Path


def _reload_alignment(common):
    if "alignment_snapshot" in sys.modules:
        importlib.reload(sys.modules["alignment_snapshot"])
    else:
        import alignment_snapshot  # noqa: F401
    return sys.modules["alignment_snapshot"]


def _seed_state(brain_root: Path, content: str | None = None) -> Path:
    """meta/alignment_state.md を仕込む (デフォルトは初期スナップショット 1 個入り)"""
    state = brain_root / "data" / "brain" / "meta" / "alignment_state.md"
    state.parent.mkdir(parents=True, exist_ok=True)
    state.write_text(
        content
        or (
            "---\n"
            "type: alignment_snapshot\n"
            "updated: 2026-04-27\n"
            "snapshot_index: 0\n"
            "clone_visibility: private\n"
            "exit_visibility: private\n"
            "---\n"
            "# Alignment State\n\n"
            "## スナップショット (時系列、新しいものを上に追記)\n\n"
            "### 2026-04 (初期スナップショット)\n\n"
            "#### 直近の関心 (2026-04)\n- foo\n- bar\n\n"
            "#### 主要指標 (2026-04)\n- raw 件数: 10\n"
        ),
        encoding="utf-8",
    )
    return state


def test_existing_months_finds_yyyy_mm(common, brain_root):
    al = _reload_alignment(common)
    text = (
        "## スナップショット (...)\n\n"
        "### 2026-05\n#### foo\n\n"
        "### 2026-04 (初期スナップショット)\n#### bar\n"
    )
    months = al.existing_months(text)
    assert months == {"2026-05", "2026-04"}


def test_existing_months_empty(common, brain_root):
    al = _reload_alignment(common)
    assert al.existing_months("# nothing here\n") == set()


def test_render_snapshot_block_format(common, brain_root):
    al = _reload_alignment(common)
    metrics = al.AlignmentMetrics(
        raw_conversations=10,
        raw_notes=212,
        wiki_style=4,
        wiki_judgment=3,
        wiki_reflex=0,
        wiki_embodiment=0,
        wiki_decisions=13,
        wiki_knowledge=29,
        wiki_people=124,
        wiki_projects=103,
        drift_entries_recent=0,
        audit_pending=7,
        audit_resolved=0,
    )
    content = {
        "interests": ["OWNDAYS 売上", "うみやまAI"],
        "judgment_trends": ["80% 実行"],
        "language_changes": [],
        "reflex_changes": [],
        "embodiment_notes": [],
        "free_text": "今月は新規 style パターンが 2 件追加された。",
    }
    block = al.render_snapshot_block("2026-05", content, metrics)
    assert block.startswith("### 2026-05\n")
    assert "#### 直近の関心 (2026-05)" in block
    assert "- OWNDAYS 売上" in block
    assert "- 80% 実行" in block
    # 空配列はフォールバック表示
    assert "(該当なし)" in block
    # メトリクスは決定論的に展開される
    assert "raw/conversations: **10 件**" in block
    assert "audit pending: **7 件**" in block


def test_render_snapshot_block_with_note(common, brain_root):
    al = _reload_alignment(common)
    metrics = al.AlignmentMetrics(
        raw_conversations=0, raw_notes=0, wiki_style=0, wiki_judgment=0,
        wiki_reflex=0, wiki_embodiment=0, wiki_decisions=0, wiki_knowledge=0,
        wiki_people=0, wiki_projects=0, drift_entries_recent=0,
        audit_pending=0, audit_resolved=0,
    )
    block = al.render_snapshot_block(
        "2026-05",
        {"interests": ["x"], "judgment_trends": [], "language_changes": [],
         "reflex_changes": [], "embodiment_notes": [], "free_text": ""},
        metrics,
        note="自動生成 (Step 8)",
    )
    assert block.startswith("### 2026-05 (自動生成 (Step 8))\n")


def test_insert_snapshot_at_top(common, brain_root):
    al = _reload_alignment(common)
    state = (
        "## スナップショット (時系列、新しいものを上に追記)\n\n"
        "### 2026-04 (初期スナップショット)\n\n"
        "#### 関心 (2026-04)\n- old\n"
    )
    new_block = "### 2026-05\n\n#### 関心 (2026-05)\n- new\n"
    out = al.insert_snapshot(state, new_block)
    # 新ブロックが旧ブロックより前に来る
    i_new = out.index("### 2026-05")
    i_old = out.index("### 2026-04")
    assert i_new < i_old
    # section header は破壊されてない
    assert "## スナップショット (時系列、新しいものを上に追記)" in out


def test_insert_snapshot_creates_section_if_missing(common, brain_root):
    al = _reload_alignment(common)
    state = "---\nfoo: bar\n---\n# Alignment\n"
    block = "### 2026-05\n#### x\n- a\n"
    out = al.insert_snapshot(state, block)
    assert "## スナップショット" in out
    assert "### 2026-05" in out


def test_remove_month(common, brain_root):
    al = _reload_alignment(common)
    state = (
        "## スナップショット (時系列、新しいものを上に追記)\n\n"
        "### 2026-05\n\n#### 関心 (2026-05)\n- new\n\n"
        "### 2026-04 (初期スナップショット)\n\n#### 関心 (2026-04)\n- old\n"
    )
    out = al.remove_month(state, "2026-05")
    assert "### 2026-05" not in out
    assert "### 2026-04 (初期スナップショット)" in out
    # 2026-04 配下の content も残る
    assert "- old" in out


def test_update_state_frontmatter_increments_index(common, brain_root):
    al = _reload_alignment(common)
    state = (
        "---\n"
        "type: alignment_snapshot\n"
        "updated: 2026-04-27\n"
        "snapshot_index: 3\n"
        "---\n"
        "# body\n"
    )
    out = al.update_state_frontmatter(state, "2026-05-01")
    assert "updated: 2026-05-01" in out
    assert "snapshot_index: 4" in out


def test_extract_prior_snapshot_picks_latest(common, brain_root):
    al = _reload_alignment(common)
    state = (
        "## スナップショット (...)\n\n"
        "### 2026-06\n\n#### 関心 (2026-06)\n- jun\n\n"
        "### 2026-05\n\n#### 関心 (2026-05)\n- may\n\n"
        "### 2026-04 (初期スナップショット)\n#### 関心 (2026-04)\n- apr\n"
    )
    # 今月 = 2026-07 (まだ存在しない)
    prior = al.extract_prior_snapshot(state, "2026-07")
    assert prior.startswith("### 2026-06")
    assert "- jun" in prior
    # 今月 = 2026-06 (存在する) → 自分は除外して 2026-05 を返す
    prior = al.extract_prior_snapshot(state, "2026-06")
    assert prior.startswith("### 2026-05")
    assert "- may" in prior


def test_extract_prior_snapshot_empty_when_first_run(common, brain_root):
    al = _reload_alignment(common)
    state = "## スナップショット (...)\n\n"
    assert al.extract_prior_snapshot(state, "2026-05") == ""


def test_gather_metrics_counts_files(common, brain_root):
    al = _reload_alignment(common)
    # raw を数件作る
    for i in range(3):
        (brain_root / "data" / "brain" / "raw" / "conversations" / f"c-{i}.md").write_text("x", encoding="utf-8")
    for i in range(7):
        (brain_root / "data" / "brain" / "raw" / "notes" / f"n-{i}.md").write_text("x", encoding="utf-8")
    # wiki/style に 2 件
    style_dir = brain_root / "data" / "brain" / "wiki" / "style"
    (style_dir / "style-001.md").write_text("---\nid: s1\n---\nx", encoding="utf-8")
    (style_dir / "style-002.md").write_text("---\nid: s2\n---\nx", encoding="utf-8")
    # _intro.md は除外
    (style_dir / "_intro.md").write_text("---\n---\nx", encoding="utf-8")
    metrics = al.gather_metrics(days=35)
    assert metrics.raw_conversations == 3
    assert metrics.raw_notes == 7
    assert metrics.wiki_style == 2  # _intro.md は除外
    assert metrics.wiki_judgment == 0


def test_gather_metrics_counts_audit_pending(common, brain_root):
    al = _reload_alignment(common)
    pending = brain_root / "data" / "brain" / "audit" / "pending_questions.md"
    pending.write_text(
        "## Q-001\n- **状態**: pending\n\n## Q-002\n- **状態**: pending\n\n"
        "## Q-003\n- **状態**: resolved\n",
        encoding="utf-8",
    )
    # resolved/ ディレクトリに 1 ファイル
    res = brain_root / "data" / "brain" / "audit" / "resolved"
    (res / "Q-099.md").write_text("done", encoding="utf-8")
    metrics = al.gather_metrics()
    assert metrics.audit_pending == 2
    assert metrics.audit_resolved == 1


def test_run_skip_when_month_exists(common, brain_root, monkeypatch):
    al = _reload_alignment(common)
    _seed_state(brain_root)

    # request_snapshot を呼んだら test fail させる
    async def _should_not_call(*args, **kwargs):
        raise AssertionError("LLM should not be called when month exists")

    monkeypatch.setattr(al, "request_snapshot", _should_not_call)

    result = asyncio.run(
        al.run(month="2026-04", apply=True, force=False, model="smart", days=35, note="")
    )
    assert result["status"] == "already_exists"


def test_run_apply_inserts_block(common, brain_root, monkeypatch):
    al = _reload_alignment(common)
    state_path = _seed_state(brain_root)

    async def _stub_llm(month, aggregated, recent_patterns, metrics,
                       prior_snapshot_excerpt="", model="smart", timeout=180.0):
        return {
            "interests": ["新規 style 抽出", "audit dedup 実装"],
            "judgment_trends": ["観測 > 推測"],
            "language_changes": [],
            "reflex_changes": [],
            "embodiment_notes": [],
            "free_text": "今月は self-replication 基盤が形になった。",
        }

    monkeypatch.setattr(al, "request_snapshot", _stub_llm)

    result = asyncio.run(
        al.run(month="2026-05", apply=True, force=False, model="smart", days=35, note="自動生成 (Step 8)")
    )
    assert result["status"] == "applied"
    assert result["month"] == "2026-05"
    assert result["block_chars"] > 0

    text = state_path.read_text(encoding="utf-8")
    # 2026-05 が追加されている
    assert "### 2026-05 (自動生成 (Step 8))" in text
    # 既存 2026-04 は保持
    assert "### 2026-04 (初期スナップショット)" in text
    # 順序: 2026-05 が上 (新しい方が上)
    i_05 = text.index("### 2026-05")
    i_04 = text.index("### 2026-04")
    assert i_05 < i_04
    # frontmatter の snapshot_index が +1 (元 0 → 1)
    assert "snapshot_index: 1" in text
    # updated が今日になる
    assert f"updated: {date.today().isoformat()}" in text


def test_run_force_replaces_existing(common, brain_root, monkeypatch):
    al = _reload_alignment(common)
    state_path = _seed_state(brain_root)

    async def _stub_llm(month, aggregated, recent_patterns, metrics,
                       prior_snapshot_excerpt="", model="smart", timeout=180.0):
        return {
            "interests": ["新しい関心 X"],
            "judgment_trends": [],
            "language_changes": [],
            "reflex_changes": [],
            "embodiment_notes": [],
            "free_text": "上書きテスト",
        }

    monkeypatch.setattr(al, "request_snapshot", _stub_llm)

    # 既存 2026-04 を上書き
    result = asyncio.run(
        al.run(month="2026-04", apply=True, force=True, model="smart", days=35, note="overwrite")
    )
    assert result["status"] == "applied"

    text = state_path.read_text(encoding="utf-8")
    # 旧 "(初期スナップショット)" は消えて、新しい note "(overwrite)" になる
    assert "(初期スナップショット)" not in text
    assert "### 2026-04 (overwrite)" in text
    assert "新しい関心 X" in text


def test_run_dry_run_does_not_modify(common, brain_root, monkeypatch, capsys):
    al = _reload_alignment(common)
    state_path = _seed_state(brain_root)
    original = state_path.read_text(encoding="utf-8")

    async def _stub_llm(month, aggregated, recent_patterns, metrics,
                       prior_snapshot_excerpt="", model="smart", timeout=180.0):
        return {
            "interests": ["dry-run 関心"],
            "judgment_trends": [],
            "language_changes": [],
            "reflex_changes": [],
            "embodiment_notes": [],
            "free_text": "",
        }

    monkeypatch.setattr(al, "request_snapshot", _stub_llm)

    result = asyncio.run(
        al.run(month="2026-05", apply=False, force=False, model="smart", days=35, note="")
    )
    assert result["status"] == "dry_run"
    # ファイルは変化していない
    assert state_path.read_text(encoding="utf-8") == original
    # stdout にブロックが流れている
    out = capsys.readouterr().out
    assert "### 2026-05" in out


def test_run_creates_state_file_if_missing(common, brain_root, monkeypatch):
    al = _reload_alignment(common)
    # state ファイルを作らない
    state_path = brain_root / "data" / "brain" / "meta" / "alignment_state.md"
    assert not state_path.exists()

    async def _stub_llm(month, aggregated, recent_patterns, metrics,
                       prior_snapshot_excerpt="", model="smart", timeout=180.0):
        return {
            "interests": ["first"],
            "judgment_trends": [],
            "language_changes": [],
            "reflex_changes": [],
            "embodiment_notes": [],
            "free_text": "",
        }

    monkeypatch.setattr(al, "request_snapshot", _stub_llm)

    result = asyncio.run(
        al.run(month="2026-05", apply=True, force=False, model="smart", days=35, note="")
    )
    assert result["status"] == "applied"
    assert state_path.exists()
    text = state_path.read_text(encoding="utf-8")
    assert "type: alignment_snapshot" in text
    assert "### 2026-05" in text


def test_count_drift_recent(common, brain_root):
    al = _reload_alignment(common)
    drift = brain_root / "data" / "brain" / "meta" / "drift_log.md"
    today = date.today().isoformat()
    drift.write_text(
        f"# drift_log\n\n## {today}\n- entry today\n\n## 2024-01-01\n- old\n",
        encoding="utf-8",
    )
    n = al._count_drift_recent(days=35)
    assert n == 1  # today だけ window 内


def test_gather_recent_patterns_only_window(common, brain_root):
    """日付 window 外のファイルは含まれない (mtime ベース)。"""
    import os
    import time
    al = _reload_alignment(common)
    style_dir = brain_root / "data" / "brain" / "wiki" / "style"
    fresh = style_dir / "style-fresh-001.md"
    stale = style_dir / "style-stale-001.md"
    fresh.write_text("---\nid: style-fresh-001\npattern: fresh thing\n---\nbody", encoding="utf-8")
    stale.write_text("---\nid: style-stale-001\npattern: stale thing\n---\nbody", encoding="utf-8")
    # stale を 200 日前にする
    old = time.time() - 200 * 86400
    os.utime(stale, (old, old))

    patterns = al.gather_recent_patterns(days=35)
    style_items = patterns["style"]
    assert any("style-fresh-001" in s for s in style_items)
    assert not any("style-stale-001" in s for s in style_items)
