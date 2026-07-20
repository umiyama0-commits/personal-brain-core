"""smoke test: clone_audit.py (★2026-05-24 Feature 3/4 海山 daily audit UI)

verdict parse + record + stats + list_recent_unrated の sanity test。
"""
from __future__ import annotations

import importlib
import json
import sys
from datetime import datetime, timedelta
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(REPO))


@pytest.mark.smoke
def test_parse_verdict_single_char():
    """1 文字 verdict: ○ × ! → 識別される。"""
    from clone_audit import parse_verdict_prefix
    assert parse_verdict_prefix("○") == ("good", "")
    assert parse_verdict_prefix("◯") == ("good", "")
    assert parse_verdict_prefix("×") == ("bad", "")
    assert parse_verdict_prefix("✕") == ("bad", "")
    assert parse_verdict_prefix("❌") == ("bad", "")
    assert parse_verdict_prefix("!") == ("fix", "")


@pytest.mark.smoke
def test_parse_verdict_with_rest():
    """verdict + rest (= note or index)."""
    from clone_audit import parse_verdict_prefix
    assert parse_verdict_prefix("× 数字古い") == ("bad", "数字古い")
    assert parse_verdict_prefix("○ 1") == ("good", "1")
    assert parse_verdict_prefix("! 正しくは 22M") == ("fix", "正しくは 22M")


@pytest.mark.smoke
def test_parse_verdict_emoji():
    """絵文字 verdict: 👍 / 👎。"""
    from clone_audit import parse_verdict_prefix
    assert parse_verdict_prefix("👍") == ("good", "")
    assert parse_verdict_prefix("👎 数字違う") == ("bad", "数字違う")


@pytest.mark.smoke
def test_parse_verdict_words():
    """単語 prefix: good / bad / fix / ok / NG 等。"""
    from clone_audit import parse_verdict_prefix
    assert parse_verdict_prefix("good") == ("good", "")
    assert parse_verdict_prefix("bad note") == ("bad", "note")
    assert parse_verdict_prefix("fix 修正内容") == ("fix", "修正内容")
    assert parse_verdict_prefix("OK") == ("good", "")
    assert parse_verdict_prefix("NG 古い") == ("bad", "古い")
    assert parse_verdict_prefix("修正 これ") == ("fix", "これ")


@pytest.mark.smoke
def test_parse_verdict_non_verdict():
    """普通のメッセージは None。"""
    from clone_audit import parse_verdict_prefix
    assert parse_verdict_prefix("普通のメッセージ") is None
    assert parse_verdict_prefix("hello") is None
    assert parse_verdict_prefix("") is None
    assert parse_verdict_prefix(None) is None
    # word prefix だが verdict 集合外
    assert parse_verdict_prefix("yes_but_not") is None


@pytest.mark.smoke
def test_record_audit_creates_file(brain_root):
    """record_audit で日別 jsonl + audited_ids 両方 update。"""
    import clone_audit
    importlib.reload(clone_audit)
    rec = clone_audit.record_audit(
        audited_by="海山_lineid",
        target_user_id="user_test",
        user_query="売上どう?",
        bot_response="今日は 20M です",
        verdict="good",
    )
    assert rec["verdict"] == "good"
    assert "id" in rec
    assert "msg_id" in rec
    # 日別 file 存在
    files = list(clone_audit.AUDIT_DIR.glob("*.jsonl"))
    assert len(files) >= 1
    # audited_ids 登録
    audited = clone_audit._load_audited_ids()
    assert rec["msg_id"] in audited


@pytest.mark.smoke
def test_record_audit_with_note(brain_root):
    """fix verdict + note 保存。"""
    import clone_audit
    importlib.reload(clone_audit)
    rec = clone_audit.record_audit(
        audited_by="海山_lineid",
        target_user_id="user_test",
        user_query="客単価は?",
        bot_response="平均 8000 円",
        verdict="fix",
        note="正しくは 8500 円、6 月以降",
    )
    assert rec["verdict"] == "fix"
    assert "8500" in rec["note"]


@pytest.mark.smoke
def test_record_audit_invalid_verdict(brain_root):
    """invalid verdict は ValueError。"""
    import clone_audit
    importlib.reload(clone_audit)
    with pytest.raises(ValueError):
        clone_audit.record_audit(
            audited_by="x", target_user_id="y",
            user_query="q", bot_response="r",
            verdict="invalid",
        )


@pytest.mark.smoke
def test_is_msg_audited_dedup(brain_root):
    """is_msg_audited で重複検出。"""
    import clone_audit
    importlib.reload(clone_audit)
    ts = "2026-05-24T10:00:00+09:00"
    user_id = "u_dedup"
    bot_response = "test response"

    # 最初は未 audit
    assert clone_audit.is_msg_audited(ts, user_id, bot_response) is False

    # record 後は audited
    clone_audit.record_audit(
        audited_by="x", target_user_id=user_id,
        user_query="q", bot_response=bot_response,
        verdict="good", ts_target=ts,
    )
    assert clone_audit.is_msg_audited(ts, user_id, bot_response) is True


@pytest.mark.smoke
def test_audit_stats_empty(brain_root):
    """audit 0 件 → all 0、good_rate_pct 0。"""
    import clone_audit
    importlib.reload(clone_audit)
    stats = clone_audit.audit_stats(days=30)
    assert stats["n_total_audits"] == 0
    assert stats["good_rate_pct"] == 0
    assert stats["needs_attention"] == []


@pytest.mark.smoke
def test_audit_stats_mixed(brain_root):
    """5 good + 2 bad + 1 fix → stats 正確。"""
    import clone_audit
    importlib.reload(clone_audit)
    for i in range(5):
        clone_audit.record_audit(
            audited_by="x", target_user_id=f"u{i}",
            user_query=f"q{i}", bot_response=f"r{i} good",
            verdict="good",
        )
    for i in range(2):
        clone_audit.record_audit(
            audited_by="x", target_user_id=f"u_bad_{i}",
            user_query=f"q{i}", bot_response=f"r{i} bad",
            verdict="bad", note="間違い",
        )
    clone_audit.record_audit(
        audited_by="x", target_user_id="u_fix",
        user_query="q", bot_response="r fix",
        verdict="fix", note="正しくは X",
    )
    stats = clone_audit.audit_stats(days=30)
    assert stats["n_total_audits"] == 8
    assert stats["n_good"] == 5
    assert stats["n_bad"] == 2
    assert stats["n_fix"] == 1
    assert stats["good_rate_pct"] == 62.5
    # needs_attention に bad + fix が入る
    assert len(stats["needs_attention"]) == 3


@pytest.mark.smoke
def test_list_recent_unrated_empty(brain_root):
    """clone_history 無し → 空 list。"""
    import clone_audit
    importlib.reload(clone_audit)
    result = clone_audit.list_recent_unrated()
    assert result == []


@pytest.mark.smoke
def test_list_recent_unrated_with_history(brain_root):
    """clone_history に bot reply あれば未 audit として返る。"""
    import clone_audit
    import clone_history
    importlib.reload(clone_history)
    importlib.reload(clone_audit)

    # clone_history に user + assistant 1 pair 書き込み
    clone_history.append("u_test", "user", "売上どう?", user_display="Tanaka")
    clone_history.append("u_test", "assistant", "20M です", user_display="Tanaka")

    result = clone_audit.list_recent_unrated(limit=10)
    assert len(result) >= 1
    item = result[0]
    assert "20M" in item["bot_response"]
    assert item["index"] == 1


@pytest.mark.smoke
def test_list_recent_unrated_filters_audited(brain_root):
    """audit 済 reply は list から除外される。"""
    import clone_audit
    import clone_history
    importlib.reload(clone_history)
    importlib.reload(clone_audit)

    import time
    clone_history.append("u_a", "user", "test", user_display="A")
    clone_history.append("u_a", "assistant", "reply_already_audited", user_display="A")
    # ★秒精度 timestamp の衝突回避: list_recent_unrated は ts で assistant を dedup するため、
    #   高速環境で 2 reply が同一秒になると一方が落ちる (別 reply の取りこぼし)。
    time.sleep(1.1)
    clone_history.append("u_b", "user", "test2", user_display="B")
    clone_history.append("u_b", "assistant", "reply_pending_audit", user_display="B")

    # u_a の reply を先 audit
    history_dir = clone_audit.BRAIN_ROOT / "clone_history"
    u_a_file = history_dir / "u_a.jsonl"
    lines = u_a_file.read_text(encoding="utf-8").strip().splitlines()
    last_rec = json.loads(lines[-1])
    clone_audit.record_audit(
        audited_by="海山", target_user_id="u_a",
        user_query="test", bot_response="reply_already_audited",
        verdict="good", ts_target=last_rec["timestamp"],
    )

    # list_recent_unrated は u_b のみ返す
    result = clone_audit.list_recent_unrated(limit=10)
    pending_responses = [r["bot_response"] for r in result]
    assert any("reply_pending_audit" in r for r in pending_responses)
    assert not any("reply_already_audited" in r for r in pending_responses)
