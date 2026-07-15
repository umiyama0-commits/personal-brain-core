"""tests/test_quality_metrics.py — 品質 metric 集計 + degradation 検知 test

★2026-05-26 海山 C2+C3: response_quality / retrieval fallback rate / quality_judge 3 軸 を
日次集計 → baseline (= 過去 7 日平均) 比較 → 20% 以上 degradation で alert。
"""
from __future__ import annotations

import importlib
import json
import sys
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "scripts"))

JST = timezone(timedelta(hours=9))


@pytest.fixture
def tmp_brain(tmp_path, monkeypatch):
    monkeypatch.setenv("BRAIN_APP_ROOT", str(tmp_path))
    monkeypatch.setenv("BRAIN_ROOT", str(tmp_path))
    if "quality_metrics" in sys.modules:
        del sys.modules["quality_metrics"]
    mod = importlib.import_module("quality_metrics")
    # ★2026-07-10 (世界基準評価 #6): 実出力に合わせ bot_events/events.jsonl (subdir)。
    #   旧 test は tmp/bot_events.jsonl (直下) に書いていたが、これが本番の path drift bug
    #   (31日 全ゼロ) と同じ誤り。code は _bot_events_path() = BRAIN_ROOT/bot_events/events.jsonl。
    mod.EVENTS_FILE = tmp_path / "bot_events" / "events.jsonl"
    mod.EVENTS_FILE.parent.mkdir(parents=True, exist_ok=True)
    mod.RESPONSE_QUALITY_DIR = tmp_path / "clone_improve" / "response_quality"
    mod.LEARNING_DIR = tmp_path / "clone_learning"
    mod.METRICS_FILE = tmp_path / "clone_improve" / "quality_metrics.jsonl"
    mod.ALERT_LOG = tmp_path / "quality_metrics_alerts.jsonl"
    mod.LEARNING_DIR.mkdir(parents=True, exist_ok=True)
    mod.RESPONSE_QUALITY_DIR.mkdir(parents=True, exist_ok=True)
    mod.METRICS_FILE.parent.mkdir(parents=True, exist_ok=True)
    return mod


def _ts(d: date, hour: int = 10) -> str:
    """date → ISO timestamp (JST hour)"""
    return datetime(d.year, d.month, d.day, hour, 0, 0, tzinfo=JST).isoformat()


def test_aggregate_bot_events_basic(tmp_brain):
    mod = tmp_brain
    target = date(2026, 5, 25)
    events = [
        {"ts": _ts(target), "event": "turn_started", "component": "clone_respond"},
        {"ts": _ts(target), "event": "turn_finished", "component": "clone_respond"},
        {"ts": _ts(target), "event": "turn_started", "component": "clone_respond"},
        {"ts": _ts(target), "event": "turn_failed", "component": "clone_respond"},
        {"ts": _ts(target), "event": "retrieval_fallback_triggered", "component": "clone_respond"},
        # different day, should be excluded
        {"ts": _ts(date(2026, 5, 24)), "event": "turn_started"},
    ]
    mod.EVENTS_FILE.write_text("\n".join(json.dumps(e, ensure_ascii=False) for e in events), encoding="utf-8")

    r = mod.aggregate_bot_events(target)
    assert r["n_started"] == 2
    assert r["n_finished"] == 1
    assert r["n_failed"] == 1
    assert r["n_retrieval_fallback"] == 1
    assert r["fail_rate_pct"] == 50.0
    assert r["fallback_rate_pct"] == 100.0


def test_aggregate_response_quality_judge(tmp_brain):
    mod = tmp_brain
    target = date(2026, 5, 25)
    f = mod.RESPONSE_QUALITY_DIR / f"{target.isoformat()}.jsonl"
    f.write_text("\n".join(json.dumps(r) for r in [
        {"ai_smell": 4, "mirroring_fit": 4, "length_appropriate": 5},
        {"ai_smell": 5, "mirroring_fit": 5, "length_appropriate": 4},
        {"ai_smell": 2, "mirroring_fit": 3, "length_appropriate": 5},  # degraded (ai_smell <=2)
        {"ai_smell": 3, "mirroring_fit": 1, "length_appropriate": 4},  # degraded
    ]), encoding="utf-8")
    r = mod.aggregate_response_quality_judge(target)
    assert r["n_judged"] == 4
    assert r["n_degraded"] == 2
    assert r["degraded_rate_pct"] == 50.0
    assert r["mean_ai_smell"] == 3.5  # (4+5+2+3)/4
    assert r["mean_mirroring_fit"] == 3.25  # (4+5+3+1)/4


def test_aggregate_response_quality_no_data(tmp_brain):
    mod = tmp_brain
    r = mod.aggregate_response_quality_judge(date(2026, 5, 25))
    assert r["available"] is False
    assert r["n_judged"] == 0


def test_aggregate_learning_discoveries(tmp_brain):
    mod = tmp_brain
    target = date(2026, 5, 25)
    f = mod.LEARNING_DIR / "2026-05.jsonl"
    f.write_text("\n".join(json.dumps(r, ensure_ascii=False) for r in [
        {"timestamp": _ts(target), "category": "response_quality"},
        {"timestamp": _ts(target), "category": "response_quality"},
        {"timestamp": _ts(target), "category": "discovery"},
        # different day
        {"timestamp": _ts(date(2026, 5, 24)), "category": "response_quality"},
    ]), encoding="utf-8")
    r = mod.aggregate_learning_discoveries(target)
    assert r["n_response_quality"] == 2
    assert r["n_total_discoveries"] == 3


def test_collect_daily_metrics_integrated(tmp_brain):
    mod = tmp_brain
    target = date(2026, 5, 25)
    mod.EVENTS_FILE.write_text(json.dumps({"ts": _ts(target), "event": "turn_started"}))
    m = mod.collect_daily_metrics(target)
    assert m["date"] == "2026-05-25"
    assert "turn" in m and "quality_judge" in m and "auto_discovery" in m


def test_compute_baseline(tmp_brain):
    mod = tmp_brain
    today_str = "2026-05-25"
    history = [
        {"date": today_str, "turn": {"fail_rate_pct": 10}, "quality_judge": {"mean_ai_smell": 3}, "auto_discovery": {"n_response_quality": 5}},
    ]
    # 過去 5 日
    for i in range(1, 6):
        history.append({
            "date": f"2026-05-{25-i:02d}",
            "turn": {"fail_rate_pct": 1.0, "fallback_rate_pct": 5.0},
            "quality_judge": {"mean_ai_smell": 4.0, "mean_mirroring_fit": 4.0, "mean_length_appropriate": 4.0, "degraded_rate_pct": 5.0},
            "auto_discovery": {"n_response_quality": 1},
        })
    base = mod.compute_baseline(history, today_str)
    assert base is not None
    assert base["n_days_baseline"] == 5
    assert base["mean_fail_rate_pct"] == 1.0


def test_compute_baseline_insufficient(tmp_brain):
    mod = tmp_brain
    history = [
        {"date": "2026-05-25", "turn": {"fail_rate_pct": 10}},
        {"date": "2026-05-24", "turn": {"fail_rate_pct": 1}},
    ]
    # only 1 day baseline → insufficient
    base = mod.compute_baseline(history, "2026-05-25")
    assert base is None


def test_detect_degradation_high_metric_worse(tmp_brain):
    """fail_rate が baseline +20% 以上 + 絶対差 >=0.5 → degraded"""
    mod = tmp_brain
    today = {
        "turn": {"fail_rate_pct": 5.0, "fallback_rate_pct": 5.0},
        "quality_judge": {"mean_ai_smell": 4.0, "mean_mirroring_fit": 4.0, "mean_length_appropriate": 4.0, "degraded_rate_pct": 5.0},
        "auto_discovery": {"n_response_quality": 1},
    }
    baseline = {
        "mean_fail_rate_pct": 1.0,
        "mean_fallback_rate_pct": 5.0,
        "mean_ai_smell": 4.0, "mean_mirroring_fit": 4.0, "mean_length_appropriate": 4.0,
        "mean_degraded_rate_pct": 5.0, "mean_n_response_quality": 1,
    }
    degraded = mod.detect_degradation(today, baseline)
    axis_names = [d["axis"] for d in degraded]
    assert "fail_rate_pct" in axis_names
    assert "fallback_rate_pct" not in axis_names  # no change


def test_detect_degradation_low_metric_worse(tmp_brain):
    """ai_smell mean が baseline -10% 以上 (= 大きく下) → degraded"""
    mod = tmp_brain
    today = {
        "turn": {"fail_rate_pct": 1.0, "fallback_rate_pct": 5.0},
        "quality_judge": {"mean_ai_smell": 3.0, "mean_mirroring_fit": 4.0, "mean_length_appropriate": 4.0, "degraded_rate_pct": 5.0},
        "auto_discovery": {"n_response_quality": 1},
    }
    baseline = {
        "mean_fail_rate_pct": 1.0, "mean_fallback_rate_pct": 5.0,
        "mean_ai_smell": 4.0, "mean_mirroring_fit": 4.0, "mean_length_appropriate": 4.0,
        "mean_degraded_rate_pct": 5.0, "mean_n_response_quality": 1,
    }
    degraded = mod.detect_degradation(today, baseline)
    axis_names = [d["axis"] for d in degraded]
    assert "mean_ai_smell" in axis_names


def test_detect_degradation_zero_baseline_to_nonzero(tmp_brain):
    """baseline=0 だが今日 2+ → degraded (= 0→2 で +inf% 問題を回避)"""
    mod = tmp_brain
    today = {
        "turn": {"fail_rate_pct": 1.0, "fallback_rate_pct": 5.0},
        "quality_judge": {"mean_ai_smell": 4.0, "mean_mirroring_fit": 4.0, "mean_length_appropriate": 4.0, "degraded_rate_pct": 5.0},
        "auto_discovery": {"n_response_quality": 3},  # 0 → 3
    }
    baseline = {
        "mean_fail_rate_pct": 1.0, "mean_fallback_rate_pct": 5.0,
        "mean_ai_smell": 4.0, "mean_mirroring_fit": 4.0, "mean_length_appropriate": 4.0,
        "mean_degraded_rate_pct": 5.0, "mean_n_response_quality": 0,
    }
    degraded = mod.detect_degradation(today, baseline)
    axis_names = [d["axis"] for d in degraded]
    assert "n_response_quality" in axis_names


def test_detect_degradation_healthy(tmp_brain):
    """全 metric が baseline と同等 or 改善 → degraded 無し"""
    mod = tmp_brain
    today = {
        "turn": {"fail_rate_pct": 0.5, "fallback_rate_pct": 4.0},  # improved
        "quality_judge": {"mean_ai_smell": 4.2, "mean_mirroring_fit": 4.1, "mean_length_appropriate": 4.0, "degraded_rate_pct": 3.0},
        "auto_discovery": {"n_response_quality": 1},
    }
    baseline = {
        "mean_fail_rate_pct": 1.0, "mean_fallback_rate_pct": 5.0,
        "mean_ai_smell": 4.0, "mean_mirroring_fit": 4.0, "mean_length_appropriate": 4.0,
        "mean_degraded_rate_pct": 5.0, "mean_n_response_quality": 1,
    }
    degraded = mod.detect_degradation(today, baseline)
    assert degraded == []


def test_append_and_load_metrics(tmp_brain):
    mod = tmp_brain
    for i in range(5):
        d = date(2026, 5, 20) + timedelta(days=i)
        mod.append_metrics({"date": d.isoformat(), "turn": {"fail_rate_pct": i}})
    items = mod.load_recent_metrics(days=14)
    assert len(items) == 5
    # newest first
    assert items[0]["date"] == "2026-05-24"


def test_run_once_dry_run_no_alert(tmp_brain, monkeypatch):
    """data が無い場合 dry-run で alert 出ない"""
    mod = tmp_brain
    n = mod.run_once(target_date=date(2026, 5, 25), dry_run=True)
    assert n == 0


def test_cron_wrapper_exists_and_executable():
    wrap = REPO_ROOT / "scripts" / "quality_metrics_cron.sh"
    assert wrap.exists()
    assert wrap.read_text().startswith("#!/")
    assert "quality_metrics.py" in wrap.read_text()
    assert "cron_env.sh" in wrap.read_text()


def test_cron_install_registers_quality_metrics():
    src = (REPO_ROOT / "scripts" / "cron_install.sh").read_text()
    assert "quality_metrics_cron.sh" in src
    assert "5 4 * * *" in src  # daily 04:05


def test_nav_includes_quality_link():
    from services.review_dashboard import _nav
    html = _nav("/admin/review/learning", "test-token")
    assert "/admin/review/quality" in html
    assert "品質" in html


def test_render_quality_page_no_data(tmp_brain):
    from services.review_dashboard import render_quality_page
    html = render_quality_page("test-token")
    assert "品質 trend" in html
    assert "alert" in html.lower() or "履歴" in html


def test_render_quality_page_with_data(tmp_brain, monkeypatch):
    mod = tmp_brain
    for i in range(3):
        d = date(2026, 5, 23) + timedelta(days=i)
        mod.append_metrics({
            "date": d.isoformat(),
            "turn": {"n_finished": 100 + i, "fail_rate_pct": 1.5, "fallback_rate_pct": 4.0},
            "quality_judge": {"n_judged": 50, "mean_ai_smell": 4.1, "mean_mirroring_fit": 4.0, "mean_length_appropriate": 4.2, "degraded_rate_pct": 5.0},
            "auto_discovery": {"n_response_quality": 2},
        })
    from services.review_dashboard import render_quality_page
    html = render_quality_page("test-token")
    assert "2026-05-23" in html
    assert "4.1" in html  # mean_ai_smell
