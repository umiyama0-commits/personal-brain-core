"""smoke: gdrive_sync._content_is_confidential の fail-closed 回帰 (codex HIGH [2]、2026-06-29)。

fullText 検索経路で本文を検証できない (空 snippet) file は §1.9 優先で除外 (fail-closed)。
gdrive_sync は google.* を top import するため CI/MacBook では skip (本番=Mac Studio で実行)。
"""
import pytest

pytest.importorskip("googleapiclient")  # google libs 無し環境では skip

import gdrive_sync  # noqa: E402


def test_empty_snippet_is_fail_closed(monkeypatch):
    """本文取得不可 (空) → 機密扱いで除外 (旧実装の no-op fail-open を塞いだ)。"""
    monkeypatch.setattr(gdrive_sync, "_fetch_content_snippet", lambda d, f: "")
    is_conf, reason = gdrive_sync._content_is_confidential(None, {"name": "中立な名前.pdf"})
    assert is_conf is True
    assert "除外" in reason or "unverifiable" in reason


def test_clean_body_passes(monkeypatch):
    """本文が読めて機密 marker 無し → 通す (over-block しない)。"""
    monkeypatch.setattr(gdrive_sync, "_fetch_content_snippet",
                        lambda d, f: "今月の売上速報。店舗別の数字と前年比。")
    is_conf, _ = gdrive_sync._content_is_confidential(None, {"name": "売上速報.xlsx"})
    assert is_conf is False


def test_pii_body_excluded(monkeypatch):
    """本文に機密 marker (評価/相談) → 除外 (既存挙動が壊れてない)。"""
    monkeypatch.setattr(gdrive_sync, "_fetch_content_snippet",
                        lambda d, f: "従業員の人事評価コメントとメンタル相談の個別記録。")
    is_conf, _ = gdrive_sync._content_is_confidential(None, {"name": "中立.docx"})
    assert is_conf is True


def test_content_safe_filter_drops_and_preserves_order(monkeypatch):
    """★2026-07-13 latency fix (cross-check R5): §1.9 関門の並列版 content_safe_filter が
    (a) 機密 file を確実に落とし (b) 入力順 (= スコア順) を保持することを pin。
    pipeline テストは素通し stub を使うため、実 filter の単体 pin はここが唯一の防衛線。"""
    gdrive_sync._CONTENT_VERDICT_CACHE.clear()
    monkeypatch.setattr(gdrive_sync, "get_credentials", lambda: None)
    monkeypatch.setattr(gdrive_sync, "build", lambda *a, **k: object())
    verdicts = {
        "A": (False, ""), "B": (True, "content match '給与': B"),
        "C": (False, ""), "D": (False, ""),
    }
    monkeypatch.setattr(gdrive_sync, "_content_is_confidential",
                        lambda drive, f: verdicts[f["id"]])
    files = [{"id": i, "name": i, "modifiedTime": "2026-07-13T00:00:00Z"}
             for i in ("A", "B", "C", "D")]
    out = gdrive_sync.content_safe_filter(files, max_workers=3)
    assert [f["id"] for f in out] == ["A", "C", "D"]  # B 除外 + 入力順保持


def test_verdict_cache_skips_unverifiable(monkeypatch):
    """transient 失敗 (unverifiable) は cache しない = 一度の network blip で file が
    プロセス寿命の間ずっと不可視化しない (cross-check Reviewer/FC 指摘)。"""
    gdrive_sync._CONTENT_VERDICT_CACHE.clear()
    calls = {"n": 0}

    def flaky(drive, f):
        calls["n"] += 1
        if calls["n"] == 1:
            return True, "本文取得不可 → §1.9 で保守的に除外 (content unverifiable)"
        return False, ""

    monkeypatch.setattr(gdrive_sync, "_content_is_confidential", flaky)
    f = {"id": "X", "name": "X", "modifiedTime": "2026-07-13T00:00:00Z"}
    conf1, _ = gdrive_sync._content_verdict_cached(None, f)
    assert conf1 is True                       # 1 回目: fail-closed で除外 (安全側)
    conf2, _ = gdrive_sync._content_verdict_cached(None, f)
    assert conf2 is False and calls["n"] == 2  # 2 回目: cache されず再判定 → 復帰
    conf3, _ = gdrive_sync._content_verdict_cached(None, f)
    assert conf3 is False and calls["n"] == 2  # 確定 verdict は cache される
