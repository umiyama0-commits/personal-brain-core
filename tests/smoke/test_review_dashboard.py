"""smoke test: services/review_dashboard.py (★2026-05-24 Feature 6 統合 Review Dashboard)

各 render_*_page() の HTML 生成 + handle_action() の accept/reject 動作 sanity test。
"""
from __future__ import annotations

import importlib
import json
import sys
from datetime import datetime
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(REPO))


@pytest.mark.smoke
def test_module_imports():
    from services.review_dashboard import (
        render_top_page, render_learning_page, render_feedback_page,
        render_audit_page, render_research_page,
        render_memory_page, render_group_page,
        handle_action, aggregate_review_queues,
    )
    # 全 function 存在確認
    assert callable(render_top_page)
    assert callable(render_learning_page)
    assert callable(handle_action)


@pytest.mark.smoke
def test_top_page_has_nav_and_kpi(brain_root):
    from services.review_dashboard import render_top_page
    html = render_top_page(token="test_token")
    # 基本構造
    assert "<!DOCTYPE html>" in html
    assert "top-nav" in html  # v2: nav class
    # nav links に各 review page あり
    assert "/admin/review/research" in html
    assert "/admin/review/audit" in html
    assert "/admin/review/learning" in html
    assert "/admin/review/feedback" in html
    # token が link に含まれる
    assert "token=test_token" in html
    # v2: KPI section + Usage chart
    assert "Pending review" in html
    assert "Phase 1 ROI Progress" in html
    assert "日別利用数" in html or "queries / day" in html


@pytest.mark.smoke
def test_audit_page_renders_empty(brain_root):
    """audit data 無し → empty state 表示."""
    import importlib
    import clone_audit
    importlib.reload(clone_audit)
    from services.review_dashboard import render_audit_page
    html = render_audit_page(token="t")
    assert "<!DOCTYPE html>" in html
    assert "Audit" in html
    # v2: empty 状態でも crash しない、stats section あり
    assert "統計" in html or "total" in html


@pytest.mark.smoke
def test_audit_page_with_data(brain_root):
    """audit data あり → stats + needs_attention 表示."""
    import importlib
    import clone_audit
    importlib.reload(clone_audit)
    # bad audit 1 件追加 (= needs_attention に出る)
    clone_audit.record_audit(
        audited_by="x", target_user_id="u1",
        user_query="売上どう?", bot_response="20M",
        verdict="bad", note="数字古い",
    )
    from services.review_dashboard import render_audit_page
    html = render_audit_page(token="t")
    # v2: stats section
    assert "統計" in html or "total" in html
    # bad item が needs_attention 表示
    assert "数字古い" in html or "売上" in html
    # v2: 3 action buttons
    assert "Accept" in html
    assert "Reject" in html
    assert "コメント付き修正" in html or "fix" in html.lower()


@pytest.mark.smoke
def test_learning_page_renders_empty(brain_root):
    from services.review_dashboard import render_learning_page
    html = render_learning_page(token="t")
    assert "<!DOCTYPE html>" in html
    # 空でも crash しない
    assert "発見" in html or "Learning" in html


@pytest.mark.smoke
def test_feedback_page_renders_empty(brain_root):
    from services.review_dashboard import render_feedback_page
    html = render_feedback_page(token="t")
    assert "<!DOCTYPE html>" in html
    assert "修正希望" in html or "Feedback" in html


@pytest.mark.smoke
def test_research_page_with_proposals(tmp_path, monkeypatch):
    """research proposals.jsonl ありで pending 表示."""
    monkeypatch.setenv("BRAIN_APP_ROOT", str(tmp_path))
    research_dir = tmp_path / "data" / "brain" / "ai_research"
    research_dir.mkdir(parents=True, exist_ok=True)
    prop_file = research_dir / "proposals.jsonl"
    prop_file.write_text(
        json.dumps({
            "id": "2026-05-24_p01",
            "ts": "2026-05-24T09:30:00+09:00",
            "title": "New retrieval pattern X",
            "body": "Some body details",
            "status": "pending",
            "source_digest": "2026-05-24-digest.md",
        }, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )
    from services.review_dashboard import render_research_page
    html = render_research_page(token="t")
    assert "New retrieval pattern X" in html
    assert "2026-05-24_p01" in html
    # accept / reject button form
    assert 'name="action" value="accept"' in html
    assert 'name="action" value="reject"' in html


@pytest.mark.smoke
def test_memory_page_renders_with_users(brain_root):
    """clone_memory user list 表示 (= v4: clickable + 完全匿名化)."""
    import importlib
    import clone_memory
    importlib.reload(clone_memory)
    clone_memory.save("u_test_001", "## Profile\nTest\n## Ongoing\n\n## Key Facts\n\n## Preferences\n",
                      user_display="Test User", turn_count=5)
    from services.review_dashboard import render_memory_page
    html = render_memory_page(token="t")
    # ★完全匿名化: user_id も user_display も Dashboard に出ない
    assert "u_test_001" not in html or "/admin/review/memory/u_test_001" in html  # link 以外には出ない
    assert "Test User" not in html
    # 「社員 A」 alias 表示
    assert "社員 A" in html
    # clickable link は user_id 経由 (= backend identifier)
    assert "/admin/review/memory/u_test_001" in html
    assert "user-card" in html


@pytest.mark.smoke
def test_group_page_renders_with_channels(brain_root):
    """clone_group_context channel list (= v4 完全匿名化)."""
    import importlib
    import clone_group_context
    importlib.reload(clone_group_context)
    clone_group_context.save(
        "ch_test_001",
        "## Group Profile\nTest group\n## Ongoing Topics\n\n## Recent Events\n\n## Group Culture\n",
        channel_display="営業本部", turn_count=10, member_count=12,
    )
    from services.review_dashboard import render_group_page
    html = render_group_page(token="t")
    # ★完全匿名化: channel_id も display 名も Dashboard に出ない
    assert "ch_test_001" not in html
    assert "営業本部" not in html
    # 「グループ A」 alias
    assert "グループ A" in html


# ─── v3 新 features ─────────────────────────────────


@pytest.mark.smoke
def test_alphabet_label():
    """index → 社員 alias letter conversion (A-Z 後 AA, AB...)."""
    from services.review_dashboard import _alphabet_label
    assert _alphabet_label(0) == "A"
    assert _alphabet_label(1) == "B"
    assert _alphabet_label(25) == "Z"
    assert _alphabet_label(26) == "AA"
    assert _alphabet_label(27) == "AB"
    assert _alphabet_label(51) == "AZ"
    assert _alphabet_label(52) == "BA"


@pytest.mark.smoke
def test_user_alias_stable(brain_root):
    """user_alias: sorted user_id 順で 社員A, B, C 安定割当."""
    import importlib
    import clone_memory
    importlib.reload(clone_memory)
    # 3 user 投入
    clone_memory.save("u_aaa", "## Profile\nA\n## Ongoing\n\n## Key Facts\n\n## Preferences\n",
                      user_display="Alice", turn_count=1)
    clone_memory.save("u_bbb", "## Profile\nB\n## Ongoing\n\n## Key Facts\n\n## Preferences\n",
                      user_display="Bob", turn_count=1)
    clone_memory.save("u_ccc", "## Profile\nC\n## Ongoing\n\n## Key Facts\n\n## Preferences\n",
                      user_display="Charlie", turn_count=1)
    from services.review_dashboard import _user_alias
    # sorted("u_aaa", "u_bbb", "u_ccc") → 0,1,2 → A, B, C
    assert _user_alias("u_aaa") == "社員 A"
    assert _user_alias("u_bbb") == "社員 B"
    assert _user_alias("u_ccc") == "社員 C"


@pytest.mark.smoke
def test_user_alias_unknown_fallback(brain_root):
    """clone_memory に無い user_id は hash fallback、* 印付き."""
    from services.review_dashboard import _user_alias
    label = _user_alias("u_unknown_xyz")
    assert label.startswith("社員 ")
    assert "*" in label  # fallback marker
    # 同じ user_id は安定 (= deterministic hash)
    assert _user_alias("u_unknown_xyz") == _user_alias("u_unknown_xyz")
    # 異なる user_id は別 label の可能性高い (= 100% 保証ではないが)


@pytest.mark.smoke
def test_user_alias_empty():
    from services.review_dashboard import _user_alias
    assert _user_alias("") == "社員 ?"
    assert _user_alias(None) == "社員 ?"


@pytest.mark.smoke
def test_channel_alias_stable(brain_root):
    """channel_alias: sorted channel_id 順で グループA, B 安定."""
    import importlib
    import clone_group_context
    importlib.reload(clone_group_context)
    clone_group_context.save(
        "ch_aaa", "## Group Profile\nA\n## Ongoing Topics\n\n## Recent Events\n\n## Group Culture\n",
        turn_count=1,
    )
    clone_group_context.save(
        "ch_bbb", "## Group Profile\nB\n## Ongoing Topics\n\n## Recent Events\n\n## Group Culture\n",
        turn_count=1,
    )
    from services.review_dashboard import _channel_alias
    assert _channel_alias("ch_aaa") == "グループ A"
    assert _channel_alias("ch_bbb") == "グループ B"


@pytest.mark.smoke
def test_mask_user_id_still_works():
    """short hash 短縮表示 (= debug 用、user identify には使わない)."""
    from services.review_dashboard import _mask_user_id
    assert _mask_user_id("Uabc1234567890") == "Uabc12…"
    assert _mask_user_id("Uabc1234567890", show_chars=8) == "Uabc1234…"
    assert _mask_user_id("short") == "short"
    assert _mask_user_id("") == "—"
    assert _mask_user_id(None) == "—"


@pytest.mark.smoke
def test_memory_detail_page_renders(brain_root):
    """個別 user memory detail page (= v4 完全匿名化)."""
    import importlib
    import clone_memory
    import clone_history
    importlib.reload(clone_memory)
    importlib.reload(clone_history)
    clone_memory.save("u_detail_001",
                      "## Profile\n営業 head\n## Ongoing Topics\n海外出店\n## Key Facts\n5 年目\n## Preferences\n数字 first\n",
                      user_display="田中 太郎", turn_count=12)
    clone_history.append("u_detail_001", "user", "売上どう?", user_display="田中 太郎")
    clone_history.append("u_detail_001", "assistant", "20M、客数 1228", user_display="田中 太郎")

    from services.review_dashboard import render_memory_detail_page
    html = render_memory_detail_page("u_detail_001", token="t")
    # ★完全匿名化: 実名 出ない
    assert "田中 太郎" not in html
    # 「社員 X」 alias 表示
    assert "社員 " in html
    # memory 内容
    assert "営業 head" in html
    assert "海外出店" in html
    # 会話履歴 bubble
    assert "売上どう?" in html
    assert "20M" in html
    assert "chat-turn" in html
    assert "bubble" in html
    # back link
    assert "/admin/review/memory?token=t" in html
    # プライバシー注記
    assert "プライバシー注記" in html or "完全匿名化" in html


@pytest.mark.smoke
def test_memory_detail_page_no_memory(brain_root):
    """memory 無し user で empty state."""
    from services.review_dashboard import render_memory_detail_page
    html = render_memory_detail_page("u_nonexistent", token="t")
    assert "memory なし" in html or "memory 無し" in html


@pytest.mark.smoke
def test_memory_detail_page_empty_user_id(brain_root):
    """user_id 空 → error message."""
    from services.review_dashboard import render_memory_detail_page
    html = render_memory_detail_page("", token="t")
    assert "未指定" in html or "user_id" in html


@pytest.mark.smoke
def test_handle_action_research_accept(tmp_path, monkeypatch):
    """handle_action('research', 'accept', id) で proposals.jsonl の status 更新."""
    monkeypatch.setenv("BRAIN_APP_ROOT", str(tmp_path))
    research_dir = tmp_path / "data" / "brain" / "ai_research"
    research_dir.mkdir(parents=True, exist_ok=True)
    prop_file = research_dir / "proposals.jsonl"
    prop_file.write_text(
        json.dumps({"id": "p_test_001", "title": "X", "body": "Y", "status": "pending"},
                   ensure_ascii=False) + "\n",
        encoding="utf-8",
    )
    from services.review_dashboard import handle_action
    ok, msg = handle_action("research", "accept", "p_test_001")
    assert ok is True
    assert "accepted" in msg
    # file 更新確認
    line = prop_file.read_text(encoding="utf-8").strip()
    rec = json.loads(line)
    assert rec["status"] == "accepted"
    assert "reviewed_at" in rec


@pytest.mark.smoke
def test_handle_action_research_reject(tmp_path, monkeypatch):
    monkeypatch.setenv("BRAIN_APP_ROOT", str(tmp_path))
    research_dir = tmp_path / "data" / "brain" / "ai_research"
    research_dir.mkdir(parents=True, exist_ok=True)
    prop_file = research_dir / "proposals.jsonl"
    prop_file.write_text(
        json.dumps({"id": "p_test_002", "title": "Y", "status": "pending"},
                   ensure_ascii=False) + "\n",
        encoding="utf-8",
    )
    from services.review_dashboard import handle_action
    ok, msg = handle_action("research", "reject", "p_test_002")
    assert ok is True
    assert "rejected" in msg


@pytest.mark.smoke
def test_handle_action_unknown_queue(brain_root):
    from services.review_dashboard import handle_action
    ok, msg = handle_action("unknown", "accept", "x")
    assert ok is False
    assert "unknown" in msg.lower()


@pytest.mark.smoke
def test_handle_action_unknown_action(brain_root):
    from services.review_dashboard import handle_action
    ok, msg = handle_action("research", "delete", "x")
    assert ok is False
    assert "unknown action" in msg.lower()


@pytest.mark.smoke
def test_handle_action_research_nonexistent_id(tmp_path, monkeypatch):
    """存在しない id → False + 失敗 message."""
    monkeypatch.setenv("BRAIN_APP_ROOT", str(tmp_path))
    from services.review_dashboard import handle_action
    ok, msg = handle_action("research", "accept", "nonexistent_id")
    assert ok is False


@pytest.mark.smoke
def test_aggregate_review_queues_no_data(brain_root):
    """clone module 全部 empty → 0 件 / None 返却 (= crash しない)."""
    from services.review_dashboard import aggregate_review_queues
    data = aggregate_review_queues()
    # キーは存在、値は 0 or None
    assert "learning_pending" in data
    assert "feedback_pending" in data
    assert "audit_total_30d" in data


@pytest.mark.smoke
def test_html_escape_xss_safe(brain_root):
    """HTML escape で XSS risk な文字列が無害化される."""
    from services.review_dashboard import _escape
    assert _escape("<script>alert(1)</script>") == "&lt;script&gt;alert(1)&lt;/script&gt;"
    assert _escape('"quote"') == "&quot;quote&quot;"
    assert _escape("") == ""
    assert _escape(None) == ""


# ─── v2 新 features ─────────────────────────────────


@pytest.mark.smoke
def test_handle_action_comment_only(tmp_path, monkeypatch):
    """action='comment' は status 変更なし、note のみ記録 (research)."""
    monkeypatch.setenv("BRAIN_APP_ROOT", str(tmp_path))
    research_dir = tmp_path / "data" / "brain" / "ai_research"
    research_dir.mkdir(parents=True, exist_ok=True)
    prop_file = research_dir / "proposals.jsonl"
    prop_file.write_text(
        json.dumps({"id": "p_test_c1", "title": "X", "status": "pending"},
                   ensure_ascii=False) + "\n",
        encoding="utf-8",
    )
    from services.review_dashboard import handle_action
    ok, msg = handle_action("research", "comment", "p_test_c1", note="気になる、後で確認")
    assert ok is True
    # comment は status 維持
    rec = json.loads(prop_file.read_text(encoding="utf-8").strip())
    assert rec["status"] == "pending"  # ← 変わってない
    assert "notes" in rec
    assert rec["notes"][0]["note"] == "気になる、後で確認"


@pytest.mark.smoke
def test_handle_action_accept_with_note(tmp_path, monkeypatch):
    """action='accept' + note → status update + note 記録."""
    monkeypatch.setenv("BRAIN_APP_ROOT", str(tmp_path))
    research_dir = tmp_path / "data" / "brain" / "ai_research"
    research_dir.mkdir(parents=True, exist_ok=True)
    prop_file = research_dir / "proposals.jsonl"
    prop_file.write_text(
        json.dumps({"id": "p_test_an", "title": "Y", "status": "pending"},
                   ensure_ascii=False) + "\n",
        encoding="utf-8",
    )
    from services.review_dashboard import handle_action
    ok, msg = handle_action("research", "accept", "p_test_an", note="next sprint で")
    assert ok is True
    rec = json.loads(prop_file.read_text(encoding="utf-8").strip())
    assert rec["status"] == "accepted"
    assert rec["notes"][0]["note"] == "next sprint で"


@pytest.mark.smoke
async def test_handle_audit_action_unrated_accept(tmp_path, monkeypatch):
    """未 audit を accept (= good verdict) で record."""
    monkeypatch.setenv("BRAIN_ROOT", str(tmp_path / "data" / "brain"))
    monkeypatch.setenv("BRAIN_APP_ROOT", str(tmp_path))

    # clone_history に 1 turn pair
    import importlib
    import clone_history
    importlib.reload(clone_history)
    clone_history.append("u_test", "user", "test query", user_display="Test")
    clone_history.append("u_test", "assistant", "test response", user_display="Test")

    import clone_audit
    importlib.reload(clone_audit)

    from services.review_dashboard import handle_audit_action
    ok, msg = handle_audit_action(
        action="accept", source="unrated", index="1", note="",
    )
    assert ok is True
    # audit record 作成された
    stats = clone_audit.audit_stats(days=30)
    assert stats["n_total_audits"] >= 1
    assert stats["n_good"] >= 1


@pytest.mark.smoke
async def test_handle_audit_action_unrated_fix_requires_note(tmp_path, monkeypatch):
    """unrated + fix で note 無し → 失敗."""
    monkeypatch.setenv("BRAIN_ROOT", str(tmp_path / "data" / "brain"))
    monkeypatch.setenv("BRAIN_APP_ROOT", str(tmp_path))
    import importlib
    import clone_history
    importlib.reload(clone_history)
    clone_history.append("u_test", "assistant", "test", user_display="T")

    import clone_audit
    importlib.reload(clone_audit)

    from services.review_dashboard import handle_audit_action
    ok, msg = handle_audit_action(
        action="fix", source="unrated", index="1", note="",
    )
    assert ok is False
    assert "note 必須" in msg or "note" in msg.lower()


@pytest.mark.smoke
def test_handle_audit_action_unknown(brain_root):
    """unknown action / source → False."""
    from services.review_dashboard import handle_audit_action
    ok1, _ = handle_audit_action(action="invalid", source="unrated")
    assert ok1 is False
    ok2, _ = handle_audit_action(action="accept", source="invalid_source")
    assert ok2 is False


@pytest.mark.smoke
def test_daily_chart_renders_svg():
    """_render_daily_chart で SVG 出力 (= mock data)."""
    from services.review_dashboard import _render_daily_chart
    trend = [
        {"date": "2026-05-20", "queries": 15, "failures": 1},
        {"date": "2026-05-21", "queries": 22, "failures": 0},
        {"date": "2026-05-22", "queries": 18, "failures": 0},
        {"date": "2026-05-23", "queries": 30, "failures": 2},
        {"date": "2026-05-24", "queries": 25, "failures": 1},
    ]
    svg = _render_daily_chart(trend)
    assert "<svg" in svg
    assert "linearGradient" in svg
    assert "circle" in svg  # data points


@pytest.mark.smoke
def test_daily_chart_empty_returns_empty_state():
    from services.review_dashboard import _render_daily_chart
    assert "empty" in _render_daily_chart([])
    assert "なし" in _render_daily_chart([])


@pytest.mark.smoke
def test_top_page_includes_usage_section(brain_root):
    """Top page に usage section + chart card 含まれる (= v2 機能追加)."""
    from services.review_dashboard import render_top_page
    html = render_top_page(token="t")
    # v2 新 sections
    assert "Phase 1 ROI Progress" in html
    assert "Channel split" in html
    assert "Audit 統計" in html
    assert "日別利用数" in html


@pytest.mark.smoke
def test_learning_page_has_comment_field(brain_root):
    """learning page に comment input field + 4 actions."""
    import importlib
    import clone_learning
    importlib.reload(clone_learning)
    # 1 件 pending 作る (= 直接 jsonl 書き込み)
    today = "2026-05-24"
    jf = clone_learning.LEARNING_DIR
    jf.mkdir(parents=True, exist_ok=True)
    (jf / f"{today}.jsonl").write_text(
        json.dumps({"id": f"{today}_001", "timestamp": f"{today}T10:00:00+09:00",
                    "category": "fact", "insight": "test insight",
                    "source_snippet": "snippet", "status": "pending",
                    "user_id": "u1", "user_display": "Test"},
                   ensure_ascii=False) + "\n",
        encoding="utf-8",
    )
    from services.review_dashboard import render_learning_page
    html = render_learning_page(token="t")
    # comment input field
    assert 'name="note"' in html
    assert "コメント" in html
    # 4 actions: accept/reject/noted/comment
    assert 'value="accept"' in html
    assert 'value="reject"' in html
    assert 'value="noted"' in html
    assert 'value="comment"' in html


@pytest.mark.smoke
def test_feedback_page_has_comment_field(brain_root):
    """feedback page にも comment field + 4 actions."""
    import importlib
    import clone_feedback
    importlib.reload(clone_feedback)
    today = "2026-05-24"
    jf = clone_feedback.FEEDBACK_DIR
    jf.mkdir(parents=True, exist_ok=True)
    (jf / f"{today}.jsonl").write_text(
        json.dumps({"id": f"{today}_001", "timestamp": f"{today}T10:00:00+09:00",
                    "user_id": "u1", "user_display": "Test",
                    "trigger_msg": "q", "response": "a", "feedback": "fix",
                    "status": "pending"},
                   ensure_ascii=False) + "\n",
        encoding="utf-8",
    )
    from services.review_dashboard import render_feedback_page
    html = render_feedback_page(token="t")
    assert 'name="note"' in html
    assert "コメント" in html
    assert 'value="comment"' in html
