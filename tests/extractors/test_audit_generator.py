"""audit_generator の単体テスト。"""
from __future__ import annotations

import importlib
import sys
from datetime import date, timedelta
from pathlib import Path


def _reload_audit(common):
    if "audit_generator" in sys.modules:
        importlib.reload(sys.modules["audit_generator"])
    else:
        import audit_generator  # noqa: F401
    return sys.modules["audit_generator"]


def _write_style(layer_dir: Path, slug: str, **fm_overrides):
    fm = {
        "type": "style_pattern",
        "id": slug,
        "category": "vocabulary",
        "context": "casual_chat",
        "pattern": "test pattern",
        "evidence": ["raw/notes/a.md"],
        "counter_evidence": [],
        "confidence": "medium",
        "last_updated": date.today().isoformat(),
        "clone_visibility": "public",
        "exit_visibility": "public",
    }
    fm.update(fm_overrides)
    lines = ["---"]
    for k, v in fm.items():
        if isinstance(v, list):
            lines.append(f"{k}: [{', '.join(v)}]")
        else:
            lines.append(f"{k}: {v}")
    lines.append("---")
    lines.append("# body\n")
    p = layer_dir / f"{slug}.md"
    p.write_text("\n".join(lines), encoding="utf-8")
    return p


def test_audit_clean_state(common, brain_root):
    audit = _reload_audit(common)
    findings = audit.detect_all()
    # identity_stale だけは絶対出る (identity.md がないため)
    kinds = {f.kind for f in findings}
    # empty_layer は raw が少ないので出ない
    assert "schema_violation" not in kinds
    assert "broken_link" not in kinds


def test_audit_schema_violation_high_confidence_low_evidence(common, brain_root):
    """confidence=high なのに evidence < 5 件 → schema_violation"""
    audit = _reload_audit(common)
    style_dir = brain_root / "data" / "brain" / "wiki" / "style"
    _write_style(
        style_dir,
        "style-bad-001",
        confidence="high",
        evidence=["only-one.md"],
    )
    findings = audit.detect_all()
    sv = [f for f in findings if f.kind == "schema_violation" and "style-bad-001" in str(f.file_path)]
    assert len(sv) == 1
    assert "evidence=1" in sv[0].detail


def test_audit_counter_evidence_zero(common, brain_root):
    """confidence=high なのに counter_evidence 空 → counter_evidence_zero"""
    audit = _reload_audit(common)
    style_dir = brain_root / "data" / "brain" / "wiki" / "style"
    _write_style(
        style_dir,
        "style-no-counter-001",
        confidence="high",
        evidence=["a.md", "b.md", "c.md", "d.md", "e.md"],  # ≥ 5 件
        counter_evidence=[],
    )
    findings = audit.detect_all()
    ce = [f for f in findings if f.kind == "counter_evidence_zero"]
    assert any("style-no-counter-001" in str(f.file_path) for f in ce)


def test_audit_visibility_inconsistency(common, brain_root):
    """clone_visibility=private + exit_visibility=public → visibility_inconsistency"""
    audit = _reload_audit(common)
    judgment_dir = brain_root / "data" / "brain" / "wiki" / "judgment"
    _write_style(
        judgment_dir,
        "judgment-inconsistent-001",
        clone_visibility="private",
        exit_visibility="public",
    )
    findings = audit.detect_all()
    vi = [f for f in findings if f.kind == "visibility_inconsistency"]
    assert any("judgment-inconsistent-001" in str(f.file_path) for f in vi)


def test_audit_broken_link(common, brain_root):
    """evidence path が存在しない → broken_link"""
    audit = _reload_audit(common)
    judgment_dir = brain_root / "data" / "brain" / "wiki" / "judgment"
    _write_style(
        judgment_dir,
        "judgment-with-bad-evidence-001",
        evidence=["wiki/decisions/nonexistent.md"],
    )
    findings = audit.detect_all()
    bl = [f for f in findings if f.kind == "broken_link"]
    assert any("judgment-with-bad-evidence-001" in str(f.file_path) for f in bl)


def test_audit_identity_stale_missing_last_validated(common, brain_root):
    """identity.md 自体は存在しない場合は何も flag しない"""
    audit = _reload_audit(common)
    findings = audit.detect_all()
    ids = [f for f in findings if f.kind == "identity_stale"]
    assert len(ids) == 0  # identity.md 不在 → check 自体スキップ


def test_audit_identity_stale_with_old_last_validated(common, brain_root):
    audit = _reload_audit(common)
    wiki_dir = brain_root / "data" / "brain" / "wiki"
    stale_date = (date.today() - timedelta(days=80)).isoformat()
    (wiki_dir / "identity.md").write_text(
        f"---\ntype: identity\nlast_validated: {stale_date}\n---\n# id\n",
        encoding="utf-8",
    )
    findings = audit.detect_all()
    ids = [f for f in findings if f.kind == "identity_stale"]
    assert len(ids) == 1
    assert "80 日" in ids[0].detail or "80日" in ids[0].detail or "80" in ids[0].detail


def test_audit_append_pending_dedup(common, brain_root):
    """同じ問いを 2 回連続で追記しても重複しない。"""
    audit = _reload_audit(common)
    style_dir = brain_root / "data" / "brain" / "wiki" / "style"
    _write_style(
        style_dir,
        "style-dup-001",
        confidence="high",
        evidence=["only-one.md"],
    )
    findings1 = audit.detect_all()
    n1 = audit.append_pending(findings1)
    assert n1 >= 1

    # 2 回目: 全く同じ findings で再 append → 0 件 (重複検出)
    findings2 = audit.detect_all()
    n2 = audit.append_pending(findings2)
    assert n2 == 0


def test_audit_empty_layer(common, brain_root):
    """raw が 30 件以上で wiki/<layer>/ が空 → empty_layer"""
    audit = _reload_audit(common)
    raw_conv = brain_root / "data" / "brain" / "raw" / "conversations"
    for i in range(35):
        (raw_conv / f"conv-{i:03d}.md").write_text("# x", encoding="utf-8")

    findings = audit.detect_all()
    el = [f for f in findings if f.kind == "empty_layer"]
    # style/judgment/reflex 全部空なので 3 件出るはず
    layers = {f.detail.split("/")[1].split("/")[0] if "/" in f.detail else "" for f in el}
    # detail 文字列 "wiki/style/ が空..." 形式
    assert any("style" in f.detail for f in el)
    assert any("judgment" in f.detail for f in el)
    assert any("reflex" in f.detail for f in el)
