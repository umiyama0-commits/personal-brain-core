"""smoke test: API 料金 + 課金状況 dashboard (★2026-05-29 海山指示「API料金トラック」)

海山指示 2 件:
  (1) Claude / OpenAI API 料金が急増 → 調査
  (2) ダッシュボードに各種 API 料金 + 課金状況の track 機能を追加

本 test は (2) の実装を検証:
  - services/usage_analytics.aggregate_cost() の集計ロジック (= per-turn usage → USD 推定)
  - services/review_dashboard.render_cost_page() の HTML render (空 path + data path)
  - routes/brain_api.py の route 登録 (= fastapi 未 install 環境向け source-text 検証)

scope 注記: usage は主に clone_respond turn に記録される「下限推定」。確定総額は
LiteLLM /spend (= _fetch_litellm_spend → budget gauge)。本 test は推定ロジックの正しさを担保。
"""
from __future__ import annotations

from datetime import datetime, timedelta
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent.parent


def _ev(model, component, usage, ts):
    return {"ts": ts, "event": "turn_finished", "model": model,
            "component": component, "usage": usage}


def _synthetic_events(days=3):
    """各日 5 model の turn_finished を生成 (= alias / dated 変種 / 各 provider 混在)."""
    from services.usage_analytics import JST
    now = datetime.now(JST)
    rows = [
        ("smart", "clone_respond",
         {"input_tokens": 60000, "output_tokens": 400,
          "cache_read_input_tokens": 30000, "cache_creation_input_tokens": 5000}),
        ("fast-gpt", "update_clone_memory",
         {"prompt_tokens": 2000, "completion_tokens": 150}),
        ("gpt-5.4", "response_quality_judge",
         {"prompt_tokens": 3000, "completion_tokens": 200}),
        ("claude-opus-4-8-20260528", "sleep_time_agent",
         {"input_tokens": 50000, "output_tokens": 300}),
        ("gpt-4o-2024-08-06", "video_describe",
         {"prompt_tokens": 1500, "completion_tokens": 500}),
    ]
    out = []
    for d in range(days):
        ts = (now - timedelta(days=d)).isoformat(timespec="seconds")
        for model, comp, usage in rows:
            out.append(_ev(model, comp, usage, ts))
    return out


# ─── L0: helper 単体 (= price/alias/provider 解決) ─────
@pytest.mark.smoke
def test_cost_canonical_resolves_litellm_aliases():
    """litellm alias (smart / fast-gpt 等) → PRICE_TABLE key に解決."""
    from services.usage_analytics import _cost_canonical
    assert _cost_canonical("smart") == "claude-opus-4-8"
    assert _cost_canonical("fast-gpt") == "gpt-5.4-mini"
    assert _cost_canonical("smart-gpt") == "gpt-5.4"
    assert _cost_canonical("contextualize") == "claude-haiku-4-5"


@pytest.mark.smoke
def test_cost_canonical_resolves_dated_and_prefixed_variants():
    """dated 変種 / provider prefix も canonical に解決 (= longest-prefix match)."""
    from services.usage_analytics import _cost_canonical
    assert _cost_canonical("gpt-4o-2024-08-06") == "gpt-4o"
    assert _cost_canonical("claude-opus-4-7-20260514") == "claude-opus-4-7"
    assert _cost_canonical("anthropic/claude-opus-4-7") == "claude-opus-4-7"
    # gpt-5.4-mini が gpt-5.4 より先に match (= longest key 優先)
    assert _cost_canonical("gpt-5.4-mini-2026") == "gpt-5.4-mini"


@pytest.mark.smoke
def test_cost_provider_classifies_to_two_axes():
    """provider 判定は海山の関心軸 (Anthropic vs OpenAI)."""
    from services.usage_analytics import _cost_provider
    assert _cost_provider("claude-opus-4-7") == "Anthropic (Claude)"
    assert _cost_provider("gpt-4o") == "OpenAI"
    assert _cost_provider("whisper-1") == "OpenAI"
    assert _cost_provider("text-embedding-3-small") == "OpenAI"


@pytest.mark.smoke
def test_cost_usd_uses_known_price_and_cache_tiers():
    """USD 計算が input/output/cache_read/cache_write の各単価を使う."""
    from services.usage_analytics import _cost_usd
    # Opus 4.7/4.8: in 1M=$5, out 1M=$25, cache_read 1M=$0.5, cache_write 1M=$6.25
    tk = {"input": 1_000_000, "output": 1_000_000,
          "cache_read": 1_000_000, "cache_write": 1_000_000}
    usd = _cost_usd(tk, "claude-opus-4-7")
    assert abs(usd - (5 + 25 + 0.5 + 6.25)) < 1e-6
    # 4.8 も同単価
    assert abs(_cost_usd(tk, "claude-opus-4-8") - (5 + 25 + 0.5 + 6.25)) < 1e-6


@pytest.mark.smoke
def test_cost_usd_unknown_model_uses_fallback():
    """未知 model は fallback price ($5 in / $15 out) で推定."""
    from services.usage_analytics import _cost_usd
    tk = {"input": 1_000_000, "output": 1_000_000, "cache_read": 0, "cache_write": 0}
    usd = _cost_usd(tk, "some-unknown-model-xyz")
    assert abs(usd - (5 + 15)) < 1e-6


# ─── L1: aggregate_cost 集計 (= 本機能の核心) ─────
@pytest.mark.smoke
def test_aggregate_cost_empty_returns_has_usage_data_false(monkeypatch):
    """usage 付き turn が 0 件なら has_usage_data False + note を返す (= crash しない)."""
    from services import usage_analytics as ua
    monkeypatch.setattr(ua, "_events_in_window", lambda since_sec: [])
    d = ua.aggregate_cost(since_sec=86400 * 14)
    assert d["has_usage_data"] is False
    assert "note" in d


@pytest.mark.smoke
def test_aggregate_cost_basic_totals(monkeypatch):
    """合計 USD / calls / provider split が整合する."""
    from services import usage_analytics as ua
    monkeypatch.setattr(ua, "_events_in_window", lambda since_sec: _synthetic_events(3))
    d = ua.aggregate_cost(since_sec=86400 * 14)
    assert d["has_usage_data"] is True
    # 5 model × 3 日 = 15 call
    assert d["totals"]["calls"] == 15
    # provider 別 USD 合計 == window total
    prov_sum = round(sum(p["usd"] for p in d["by_provider"]), 2)
    assert abs(prov_sum - d["totals"]["usd"]) < 0.05
    # provider pct 合計 ≈ 100
    assert abs(sum(p["pct"] for p in d["by_provider"]) - 100) < 0.5


@pytest.mark.smoke
def test_aggregate_cost_alias_and_dated_variant_merge(monkeypatch):
    """'smart' と 'claude-opus-4-8-20260528' が同一 canonical に merge される."""
    from services import usage_analytics as ua
    monkeypatch.setattr(ua, "_events_in_window", lambda since_sec: _synthetic_events(3))
    d = ua.aggregate_cost(since_sec=86400 * 14)
    models = {m["model"]: m for m in d["by_model"]}
    # smart(clone_respond) + dated(sleep_time_agent) が claude-opus-4-8 に統合 = 6 call (3 日 × 2)
    assert "claude-opus-4-8" in models
    assert models["claude-opus-4-8"]["calls"] == 6
    assert models["claude-opus-4-8"]["known_price"] is True


@pytest.mark.smoke
def test_aggregate_cost_no_other_provider_leak(monkeypatch):
    """既知 alias / model は全て Anthropic / OpenAI に分類、'other' に漏れない."""
    from services import usage_analytics as ua
    monkeypatch.setattr(ua, "_events_in_window", lambda since_sec: _synthetic_events(2))
    d = ua.aggregate_cost(since_sec=86400 * 14)
    provs = {m["provider"] for m in d["by_model"]}
    assert provs <= {"Anthropic (Claude)", "OpenAI"}, f"leak: {provs}"


@pytest.mark.smoke
def test_aggregate_cost_cache_hit_pct(monkeypatch):
    """cache hit % = cache_read / (input + cache_read + cache_write) [Anthropic 分]."""
    from services import usage_analytics as ua
    monkeypatch.setattr(ua, "_events_in_window", lambda since_sec: _synthetic_events(3))
    d = ua.aggregate_cost(since_sec=86400 * 14)
    cache = d["cache"]
    # Anthropic input = clone_respond 60000×3 + sleep 50000×3 = 330000
    assert cache["anthropic_input_tokens"] == 330000
    assert cache["anthropic_cache_read_tokens"] == 90000   # 30000×3
    assert cache["anthropic_cache_write_tokens"] == 15000  # 5000×3
    expected = round(90000 / (330000 + 90000 + 15000) * 100, 1)
    assert cache["cache_hit_pct"] == expected


@pytest.mark.smoke
def test_aggregate_cost_convention_robust_no_double_count(monkeypatch):
    """本番経路 (LiteLLM /v1/chat/completions) は OpenAI 形式に正規化し、prompt_tokens に
    cache を含める (= 通常 input + cache_read + cache_write の合算)。この convention でも
    (a) cached token を二重課金せず、(b) Anthropic-native と同一の USD / cache_hit_pct を
    出すことを担保する (★2026-05-29 二重課金 fix の regression guard)。

    旧 code は prompt_tokens(=95000) を丸ごと input 扱いした上で cache_read/write を別途
    加算 → cached 35000 token を full price と cache price で二重課金、cache_hit_pct の
    分母も膨らみ hit 率を過小報告していた。
    """
    from services import usage_analytics as ua
    from datetime import datetime
    ts = datetime.now(ua.JST).isoformat(timespec="seconds")

    # 論理的に同一の Opus turn: uncached input 60000 / cache_read 30000 / cache_write 5000。
    native = _ev("smart", "clone_respond",
                 {"input_tokens": 60000, "output_tokens": 400,
                  "cache_read_input_tokens": 30000, "cache_creation_input_tokens": 5000}, ts)
    # OpenAI/LiteLLM: prompt_tokens = 60000+30000+5000 = 95000 (cache 込み合算)。
    openai = _ev("smart", "clone_respond",
                 {"prompt_tokens": 95000, "completion_tokens": 400,
                  "cache_read_input_tokens": 30000, "cache_creation_input_tokens": 5000}, ts)
    # OpenAI nested: cache read を prompt_tokens_details.cached_tokens に格納する形式。
    nested = _ev("smart", "clone_respond",
                 {"prompt_tokens": 95000, "completion_tokens": 400,
                  "prompt_tokens_details": {"cached_tokens": 30000},
                  "cache_creation_input_tokens": 5000}, ts)

    def agg(events):
        monkeypatch.setattr(ua, "_events_in_window", lambda since_sec: events)
        return ua.aggregate_cost(since_sec=86400 * 14)

    d_native, d_openai, d_nested = agg([native]), agg([openai]), agg([nested])

    # uncached input / cache 分が 3 convention で一致 (= 60000 / 30000 / 5000)。
    for d in (d_native, d_openai, d_nested):
        assert d["cache"]["anthropic_input_tokens"] == 60000
        assert d["cache"]["anthropic_cache_read_tokens"] == 30000
        assert d["cache"]["anthropic_cache_write_tokens"] == 5000

    # 正しい USD = uncached 60000×$5 + out 400×$25 + cr 30000×$0.5 + cw 5000×$6.25。
    correct = round((60000 * 5 + 400 * 25 + 30000 * 0.5 + 5000 * 6.25) / 1_000_000, 2)
    usd_native = d_native["totals"]["usd"]
    assert abs(usd_native - correct) < 0.01
    assert d_openai["totals"]["usd"] == usd_native    # convention 不変
    assert d_nested["totals"]["usd"] == usd_native

    # 旧 (二重課金) code は input=95000 で課金 → 必ず過大。新値はそれ未満。
    buggy = (95000 * 5 + 400 * 25 + 30000 * 0.5 + 5000 * 6.25) / 1_000_000
    assert usd_native < buggy

    # cache_hit_pct も convention 不変 = 30000/(60000+30000+5000) = 31.6%。
    expected_pct = round(30000 / (60000 + 30000 + 5000) * 100, 1)
    for d in (d_native, d_openai, d_nested):
        assert d["cache"]["cache_hit_pct"] == expected_pct


@pytest.mark.smoke
def test_aggregate_cost_prompt_tokens_excludes_cache_fallback(monkeypatch):
    """旧 LiteLLM / Anthropic-native で prompt_tokens が cache を「含まない」場合の耐性。
    pt < cr+cw を算術検知して pt+cr+cw を total とみなし、uncached=pt を復元する
    (= convention 自動判定の境界 case)。"""
    from services import usage_analytics as ua
    from datetime import datetime
    ts = datetime.now(ua.JST).isoformat(timespec="seconds")
    # prompt_tokens=60000 が「uncached のみ」(cache は別 field)。60000 < 30000+5000 ではない
    # ので、より極端に: uncached 2000 / cache_read 30000 / cache_write 5000。
    ev = _ev("smart", "clone_respond",
             {"prompt_tokens": 2000, "completion_tokens": 100,
              "cache_read_input_tokens": 30000, "cache_creation_input_tokens": 5000}, ts)
    monkeypatch.setattr(ua, "_events_in_window", lambda since_sec: [ev])
    d = ua.aggregate_cost(since_sec=86400 * 14)
    # pt(2000) < cr+cw(35000) → cache 抜き扱い、uncached=2000 を復元 (95000 に膨らませない)。
    assert d["cache"]["anthropic_input_tokens"] == 2000
    assert d["cache"]["anthropic_cache_read_tokens"] == 30000
    assert d["cache"]["anthropic_cache_write_tokens"] == 5000


@pytest.mark.smoke
def test_aggregate_cost_component_breakdown_per_model_price(monkeypatch):
    """component USD は per-model price を個別適用 (= 混在 model で誤集計しない)."""
    from services import usage_analytics as ua
    monkeypatch.setattr(ua, "_events_in_window", lambda since_sec: _synthetic_events(1))
    d = ua.aggregate_cost(since_sec=86400 * 14)
    comps = {c["component"]: c for c in d["by_component"]}
    # clone_respond (Opus, 1 日 1 call) = in 60000×$5 + out 400×$25 + cr 30000×$0.5 + cw 5000×$6.25
    expected = (60000 * 5 + 400 * 25 + 30000 * 0.5 + 5000 * 6.25) / 1_000_000
    assert abs(comps["clone_respond"]["usd"] - round(expected, 2)) < 0.01
    # 最高コスト component が clone_respond (= Opus 大文脈)
    assert d["by_component"][0]["component"] in ("clone_respond", "sleep_time_agent")


@pytest.mark.smoke
def test_aggregate_cost_budget_from_env(monkeypatch):
    """日次 budget cap は LITELLM_MAX_BUDGET env から (default 50)."""
    from services import usage_analytics as ua
    monkeypatch.setattr(ua, "_events_in_window", lambda since_sec: _synthetic_events(1))
    monkeypatch.setenv("LITELLM_MAX_BUDGET", "80")
    d = ua.aggregate_cost(since_sec=86400 * 14)
    assert d["budget"]["cap_usd"] == 80.0


# ─── L2: render_cost_page (= HTML 出力) ─────
@pytest.mark.smoke
def test_render_cost_page_empty_path(monkeypatch):
    """usage data 無しでも budget gauge + 調査メモ + 蓄積中 note を render (= crash しない)."""
    from services import usage_analytics as ua
    from services.review_dashboard import render_cost_page
    monkeypatch.setattr(ua, "_events_in_window", lambda since_sec: [])
    html = render_cost_page("tok")
    assert len(html) > 1000
    assert "課金状況" in html
    assert "調査メモ" in html  # 調査メモは常時表示
    assert "API 料金" in html


@pytest.mark.smoke
def test_render_cost_page_with_data(monkeypatch):
    """usage data ありで provider / model / component / cache section を render."""
    from services import usage_analytics as ua
    from services.review_dashboard import render_cost_page
    monkeypatch.setattr(ua, "_events_in_window", lambda since_sec: _synthetic_events(3))
    html = render_cost_page("tok")
    for needle in ["Provider 別", "Model 別", "component", "cache",
                   "Anthropic", "OpenAI", "clone_respond"]:
        assert needle in html, f"missing section: {needle}"


@pytest.mark.smoke
def test_render_cost_page_gauge_uses_litellm_real_value(monkeypatch):
    """litellm_status (= /spend 実値) を渡すと budget gauge がその値を優先."""
    from services import usage_analytics as ua
    from services.review_dashboard import render_cost_page
    monkeypatch.setattr(ua, "_events_in_window", lambda since_sec: _synthetic_events(1))
    html = render_cost_page("tok", litellm_status={"used_usd": 12.34, "budget_usd": 50.0, "usage_pct": 24.7})
    assert "12.34" in html
    assert "確定" in html  # "LiteLLM /spend 確定値"


@pytest.mark.smoke
def test_render_cost_page_gauge_danger_when_over_90pct(monkeypatch):
    """spend が budget の 90% 超で danger 警告 (= 503 → fallback の注意)."""
    from services import usage_analytics as ua
    from services.review_dashboard import render_cost_page
    monkeypatch.setattr(ua, "_events_in_window", lambda since_sec: [])
    html = render_cost_page("tok", litellm_status={"used_usd": 48.0, "budget_usd": 50.0, "usage_pct": 96.0})
    assert "お休みをいただいてます" in html  # 503 fallback 警告
    assert "503" in html


# ─── L3: route 登録 (= fastapi 未 install 環境向け source-text 検証) ─────
@pytest.mark.smoke
def test_cost_routes_registered_in_source():
    """/admin/review/cost (HTML) + /api/admin/cost (JSON) + _fetch_litellm_spend helper."""
    src = (REPO / "routes" / "brain_api.py").read_text(encoding="utf-8")
    assert '@router.get("/admin/review/cost"' in src
    assert "async def admin_review_cost" in src
    assert '@router.get("/api/admin/cost")' in src
    assert "async def admin_cost_json" in src
    assert "async def _fetch_litellm_spend" in src
    # HTML route は LiteLLM /spend を取って render_cost_page に渡す
    idx = src.find("async def admin_review_cost")
    body = src[idx: idx + 1200]
    assert "render_cost_page" in body
    assert "litellm_status" in body
    # token 認証
    assert "check_at_token(token)" in body


@pytest.mark.smoke
def test_fetch_litellm_spend_no_plaintext_secret():
    """_fetch_litellm_spend は os.getenv 経由のみ、平文 secret 直書きなし (CLAUDE.md 1.1)."""
    src = (REPO / "routes" / "brain_api.py").read_text(encoding="utf-8")
    idx = src.find("async def _fetch_litellm_spend")
    body = src[idx: idx + 1500]
    # env 経由 (module-level LITELLM_KEY / LITELLM_MAX_BUDGET)
    assert "LITELLM_KEY" in body
    assert 'os.getenv("LITELLM_MAX_BUDGET"' in body
    # /spend → /spend/logs fallback
    assert "/spend" in body
    # 平文 key の典型 prefix が無い
    assert "sk-litellm" not in src
    assert "sk-ant-" not in src


@pytest.mark.smoke
def test_nav_has_cost_link():
    """dashboard nav に「API料金」link が追加されている."""
    src = (REPO / "services" / "review_dashboard.py").read_text(encoding="utf-8")
    assert '"/admin/review/cost", "API料金"' in src or '("/admin/review/cost", "API料金")' in src


@pytest.mark.smoke
def test_investigation_memo_lists_top_drivers():
    """調査メモが cost 要因 top 5 + 海山承認注記 (CLAUDE.md 1.15) を含む."""
    src = (REPO / "services" / "review_dashboard.py").read_text(encoding="utf-8")
    idx = src.find("def _cost_investigation_memo")
    body = src[idx: idx + 1800]
    # 主要 driver
    assert "wiki" in body and "cache" in body          # #1 Opus 90K wiki uncached
    assert "sleep_time_agent" in body                   # #2 double-Opus
    assert "judge" in body or "GPT-5.4" in body         # #3 quality judge
    # 対策は海山承認必須
    assert "海山承認" in body or "1.15" in body
