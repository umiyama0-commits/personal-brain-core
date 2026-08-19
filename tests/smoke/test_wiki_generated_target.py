"""tests/smoke/test_wiki_generated_target.py

★2026-08-18 loud-fail「wiki_append_size が 191 回連続」の根治を固定する。

何が起きていたか:
  LINE Works 取込 → LLM compile が書き先に **wiki/index.md** を選んでいた。
  index.md は全 wiki を列挙するカタログの **全再生成物** (write_text で毎回作り直す)
  なので、そこへの追記はそもそも意味が無い。さらに 8/17 に 120KB 上限を超えてからは
  plan_append が拒否 → `return` で **compile 出力がそのまま捨てられていた**。
  中身は 2,994 行すべてユニークで、alert が疑った「重複追記ループ」ではなかった。

固定する不変条件:
  ① 生成物は compile の書き先にならない (canonical と同じ退避の作法)
  ② 上限超過でも内容を捨てない (overflow へ退避)
"""
from __future__ import annotations

import pathlib

_ROOT = pathlib.Path(__file__).resolve().parents[2]
_BW = (_ROOT / "brain_wiki.py").read_text(encoding="utf-8")


# ─── ① 生成物は書き先にしない ────────────────────────────────────────────

def test_index_is_registered_as_generated():
    assert "_GENERATED_WIKI_FILES" in _BW
    i = _BW.index("_GENERATED_WIKI_FILES = {")
    seg = _BW[i:i + 120]
    assert '"index.md"' in seg, "index.md が生成物として登録されていない"


def test_generated_target_is_diverted_not_dropped():
    """canonical と同じく「拒否して退避」。return で捨てない。"""
    i = _BW.index("if file_path.name in _GENERATED_WIKI_FILES:")
    seg = _BW[i:i + 1000]
    assert "-discussion.md" in seg, "退避先が無い"
    assert "loud_fail" in seg or "_lf(" in seg, "黙って握りつぶしている"
    assert "file_path = divert" in seg, "書き先を退避先に差し替えていない"
    # この分岐に return が無いこと (= 捨てていない)
    assert "return" not in seg.split("file_path = divert")[0], "退避前に return している"


def test_generated_guard_runs_before_the_canonical_guard():
    """index.md は canonical: true を持たないので、canonical 判定より前に見る必要がある。"""
    assert (_BW.index("if file_path.name in _GENERATED_WIKI_FILES:")
            < _BW.index("if file_path.exists() and _is_canonical_wiki(file_path):"))


def test_generated_guard_alert_does_not_spam_daily():
    """§1.18: 毎日同文 alert は bug 扱い。threshold で間引く。"""
    i = _BW.index('_lf("generated_wiki_write"')
    seg = _BW[i:i + 400]
    assert "threshold=" in seg and "cooldown_h=" in seg


# ─── ② 上限超過でも内容を捨てない ────────────────────────────────────────

def test_size_limit_diverts_to_overflow_instead_of_discarding():
    i = _BW.index('if reason.startswith("size_limit"):')
    seg = _BW[i:i + 1800]
    assert "-overflow.md" in seg, "上限超過の内容を退避していない (捨てている)"
    assert "overflow.write_text" in seg, "退避を書き出していない"
    assert "plan_append(" in seg, "退避先にも 3 門を通していない (退避先が無限肥大する)"


def test_size_limit_alert_message_is_not_misleading():
    """旧文言は「重複追記ループの疑い」と断定していたが、実際は書き先の誤りだった。"""
    i = _BW.index('loud_fail("wiki_append_size"')
    seg = _BW[i:i + 500]
    assert "退避済" in seg, "退避したことを通知していない"
    assert "重複追記ループの疑い —" not in seg, "原因を 1 つに断定する旧文言が残っている"
