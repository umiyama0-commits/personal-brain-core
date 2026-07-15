"""clone_review jsonl queue の並行安全性 regression guard.

★2026-06-10: system_issues / data_gaps / web_clips の read-modify-write に lock が無く、
近接更新で lost update / dedupe race が起きた (ADR Codex MEDIUM)。services/_review_store.py
の fcntl lock + atomic write で直列化した修正の検証。

threading で並行させ、修正後に「全件残る・dedupe が 1 レコードに収束する」ことを確認する。
"""
import importlib
import threading


def _reload_with_tmp(modname, tmp_path, monkeypatch):
    """BRAIN_ROOT を tmp に向けて module を reload し、FILE 定数を隔離する。"""
    monkeypatch.setenv("BRAIN_ROOT", str(tmp_path))
    mod = importlib.import_module(modname)
    importlib.reload(mod)
    return mod


def test_system_issues_concurrent_add_no_loss(tmp_path, monkeypatch):
    """30 スレッド同時 add_entry → 全 30 件残る (append 競合で消えない)。"""
    si = _reload_with_tmp("services.system_issues", tmp_path, monkeypatch)
    N = 30
    threads = [threading.Thread(target=lambda i=i: si.add_entry(f"issue {i}")) for i in range(N)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert len(si._read_all()) == N


def test_system_issues_add_update_concurrent_no_loss(tmp_path, monkeypatch):
    """add と update_status が並行しても lost update が起きない。"""
    si = _reload_with_tmp("services.system_issues", tmp_path, monkeypatch)
    ids = [si.add_entry(f"base {i}") for i in range(10)]
    threads = []
    for i in range(10):
        threads.append(threading.Thread(target=lambda i=i: si.add_entry(f"new {i}")))
        threads.append(threading.Thread(target=lambda fid=ids[i]: si.update_status(fid, "acknowledged")))
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    items = si._read_all()
    assert len(items) == 20  # 10 base + 10 new、消失なし
    assert sum(1 for r in items if r.get("status") == "acknowledged") == 10  # 更新も全反映


def test_data_gaps_dedupe_no_race(tmp_path, monkeypatch):
    """同一 query を 25 並行 capture → dedupe で 1 レコード occurrence=25 に収束。

    lock 無しなら read-modify-write race で複数レコード or occurrence < 25 になる。
    """
    dg = _reload_with_tmp("services.data_gaps", tmp_path, monkeypatch)

    def cap():
        dg.auto_capture("武蔵小山の客単価は?", "データがありません", matched_category="no_data")

    threads = [threading.Thread(target=cap) for _ in range(25)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    items = dg._read_all()
    assert len(items) == 1                      # race なら複数レコードに分裂する
    assert items[0]["occurrence_count"] == 25   # 全 increment が直列化されて反映


def test_conversation_success_concurrent_record_no_loss(tmp_path, monkeypatch):
    """20 並行 record_success → 全 20 件残る (lock 横展開後の lost update なし)。"""
    cs = _reload_with_tmp("services.conversation_success", tmp_path, monkeypatch)
    N = 20
    threads = [threading.Thread(target=lambda i=i: cs.record_success(f"u{i}", None, "q", "r", "cont"))
               for i in range(N)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert sum(1 for _ in cs._iter_records()) == N
