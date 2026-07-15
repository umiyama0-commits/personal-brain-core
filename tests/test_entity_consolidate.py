"""brain_wiki_helpers.entity_consolidate.plan_write の隔離テスト(過剰分割 恒久対策、2026-07-01)。"""
import sys, pathlib
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))
from brain_wiki_helpers.entity_consolidate import plan_write

FM = "---\nupdated: 2026-06-08\nconfidence: medium\ntags: [仕事]\nsources: [raw/x.md]\n---\n"
DAY1 = FM + "# FF/CVR対策 週次定例\n\n## 決定内容\n- 6/8の決定A\n\n## 関連\n- [[projects/sales]]\n"
DAY2 = ("---\nupdated: 2026-06-09\ntags: [仕事]\nsources: [raw/y.md]\n---\n"
        "# FF/CVR対策 週次定例\n\n## 決定内容\n- 6/9の決定B\n")


def _store(initial=None):
    d = dict(initial or {})
    return d, (lambda t: t in d), (lambda t: d[t])


def test_passthrough_non_target_dir():
    tn, tc, cons = plan_write("knowledge", "2026-06-08-foo.md", "x", "create", lambda t: False, lambda t: "")
    assert cons is False and tn == "2026-06-08-foo.md" and tc == "x"


def test_passthrough_undated_filename():
    tn, tc, cons = plan_write("decisions", "ff-cvr-weekly-meeting.md", "x", "append", lambda t: True, lambda t: "y")
    assert cons is False and tn == "ff-cvr-weekly-meeting.md"


def test_first_occurrence_creates_entity_with_log():
    _, ex, rd = _store()
    tn, tc, cons = plan_write("decisions", "2026-06-08-ff-cvr-weekly-meeting.md", DAY1, "create", ex, rd)
    assert cons is True and tn == "ff-cvr-weekly-meeting.md"
    assert "# FF/CVR対策 週次定例" in tc
    assert "## 時系列ログ" in tc
    assert "### 2026-06-08" in tc and "6/8の決定A" in tc
    assert "2026-06-08-" not in tn                      # 日付プレフィックスは消える


def test_second_occurrence_appends_dated_section():
    # 1回目の生成結果を entity ページとして保持
    _, ex0, rd0 = _store()
    tn, page1, _ = plan_write("decisions", "2026-06-08-ff-cvr-weekly-meeting.md", DAY1, "create", ex0, rd0)
    store, ex, rd = _store({tn: page1})
    tn2, page2, cons = plan_write("decisions", "2026-06-09-ff-cvr-weekly-meeting.md", DAY2, "create", ex, rd)
    assert cons is True and tn2 == tn
    assert "### 2026-06-08" in page2 and "### 2026-06-09" in page2   # 両日が1ページに
    assert "6/9の決定B" in page2
    assert "updated: 2026-06-09" in page2                            # updated 更新
    # 関連は末尾に1つ、追記 section はその前に入る
    assert page2.index("### 2026-06-09") < page2.rindex("## 関連")


def test_idempotent_same_date():
    _, ex0, rd0 = _store()
    tn, page1, _ = plan_write("decisions", "2026-06-08-ff-cvr-weekly-meeting.md", DAY1, "create", ex0, rd0)
    _, ex, rd = _store({tn: page1})
    tn2, page2, cons = plan_write("decisions", "2026-06-08-ff-cvr-weekly-meeting.md", DAY1, "create", ex, rd)
    assert cons is True and page2 == page1              # 同日再取込は無変更
