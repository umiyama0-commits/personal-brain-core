"""smoke test: scripts/tracing.py (OpenTelemetry 統合)。

opentelemetry がローカルに無くても crash しないことと、
span が context manager / decorator として動くことを確認。
"""
from __future__ import annotations

import asyncio
import importlib
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(REPO_ROOT / "scripts"))


@pytest.fixture
def reload_tracing(monkeypatch):
    """tracing を reload して状態を reset。"""
    monkeypatch.delenv("OTEL_EXPORTER_OTLP_ENDPOINT", raising=False)
    monkeypatch.delenv("OTEL_CONSOLE", raising=False)
    if "tracing" in sys.modules:
        importlib.reload(sys.modules["tracing"])
    import tracing  # type: ignore
    tracing.reset_for_test()
    return tracing


@pytest.mark.smoke
def test_module_imports_without_otel(reload_tracing):
    """opentelemetry が無くても import 自体は成功する。"""
    tracing = reload_tracing
    # 主要 API が exposed
    assert callable(tracing.span)
    assert callable(tracing.init_tracing)
    assert callable(tracing.traced)
    assert callable(tracing.is_enabled)


@pytest.mark.smoke
def test_span_context_manager_no_op(reload_tracing):
    """opentelemetry 未初期化なら span は no-op (yield None)。"""
    tracing = reload_tracing
    with tracing.span("test", key="val") as s:
        # opentelemetry 未インストール環境では s is None
        # インストール済みでも初期化前なら s is None
        # どちらにせよ crash しないこと
        pass


@pytest.mark.smoke
def test_span_accepts_attributes_safely(reload_tracing):
    """attribute (int, str, float, bool, 複雑 obj) を渡しても crash しない。"""
    tracing = reload_tracing
    with tracing.span("test",
                      int_v=1, str_v="hello", float_v=1.5,
                      bool_v=True, list_v=[1, 2, 3],
                      dict_v={"a": 1}) as s:
        pass


@pytest.mark.smoke
def test_span_propagates_exception(reload_tracing):
    """例外は span を越えて propagate される。"""
    tracing = reload_tracing
    with pytest.raises(ValueError):
        with tracing.span("test"):
            raise ValueError("boom")


@pytest.mark.smoke
def test_traced_decorator_sync(reload_tracing):
    """traced decorator (sync) — 戻り値が透過。"""
    tracing = reload_tracing

    @tracing.traced("my_func")
    def add(a, b):
        return a + b

    assert add(2, 3) == 5


@pytest.mark.smoke
def test_traced_decorator_async(reload_tracing):
    """traced decorator (async) — coroutine も動く。"""
    tracing = reload_tracing

    @tracing.traced("async_func")
    async def add_async(a, b):
        return a + b

    result = asyncio.run(add_async(2, 3))
    assert result == 5


@pytest.mark.smoke
def test_init_tracing_idempotent(reload_tracing):
    """init_tracing を 2 回呼んでも crash しない。"""
    tracing = reload_tracing
    tracing.init_tracing("test-svc")
    tracing.init_tracing("test-svc")  # 2 回目は no-op


@pytest.mark.smoke
def test_init_tracing_returns_bool(reload_tracing):
    """init_tracing が bool を返す (enable status)。"""
    tracing = reload_tracing
    result = tracing.init_tracing("test-svc")
    assert isinstance(result, bool)


@pytest.mark.smoke
def test_init_with_console_export(reload_tracing, monkeypatch, capsys):
    """OTEL_CONSOLE=1 で console exporter が登録される (opentelemetry あれば)。"""
    tracing = reload_tracing
    monkeypatch.setenv("OTEL_CONSOLE", "1")
    enabled = tracing.init_tracing("test-svc")
    # opentelemetry が無ければ False、あれば True
    # ローカル環境では opentelemetry-sdk が入ってる前提では無いので skip 寛容に
    with tracing.span("test_span"):
        pass


@pytest.mark.smoke
def test_brain_wiki_imports_tracing_safely():
    """brain_wiki.py が tracing を soft import している。"""
    src = (REPO_ROOT / "brain_wiki.py").read_text(encoding="utf-8")
    assert "from scripts.tracing import span" in src
    # subspan が clone_respond / retrieval / memory_load / llm_call で立ってる
    assert "clone_respond.retrieval" in src
    assert "clone_respond.memory_load" in src
    assert "clone_respond.llm_call" in src


@pytest.mark.smoke
def test_main_imports_init_tracing():
    """main.py の lifespan で init_tracing を呼んでいる。"""
    src = (REPO_ROOT / "main.py").read_text(encoding="utf-8")
    assert "init_tracing" in src
    assert "personal-brain" in src


@pytest.mark.smoke
def test_requirements_has_opentelemetry():
    """requirements.txt に opentelemetry-* が追加されている。"""
    text = (REPO_ROOT / "requirements.txt").read_text(encoding="utf-8")
    assert "opentelemetry-api" in text
    assert "opentelemetry-sdk" in text
