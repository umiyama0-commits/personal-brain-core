"""
tracing.py — OpenTelemetry tracing 共通基盤 (★2026-05-21 追加)

目的:
  bot 応答 1 turn 内の subspan (retrieval / memory load / LLM call / scrub 等) を
  追えるようにする。bot_events.jsonl は turn 単位の latency しか取れず、
  「p95 latency のボトルネックが retrieval なのか LLM なのか scrub なのか」が
  分からなかった (世界基準で hole の 1 つ #4)。

設計:
  - soft import: opentelemetry 未インストールでも crash しない (no-op に落ちる)
  - `init_tracing(service_name)` で SDK 初期化 (main.py の startup で呼ぶ)
  - `span(name)` で context manager / decorator として使える
  - exporter:
    - OTEL_EXPORTER_OTLP_ENDPOINT が立っていれば OTLP (Jaeger / Honeycomb / Datadog 等)
    - 立ってなければ no-op (production で外部出力なし、開発時は OTEL_CONSOLE=1 で stdout)
  - sampling: 100% (Personal Brain は単一ユーザなので全 trace 取って良い)

なぜ単一ファイルか:
  bot_events.py と同じく依存最小 + どこからでも import 安全。
  本番デプロイ時に requirements.txt に opentelemetry-* を足せば自動 ON。

使い方:
  # main.py
  from scripts.tracing import init_tracing
  init_tracing("personal-brain")

  # brain_wiki.py
  from scripts.tracing import span
  with span("clone_respond.retrieval"):
      wiki_content = await self._read_wiki_state_public_compact(query)
  with span("clone_respond.llm_call", model=model):
      resp = await self.http.post(...)

  # decorator として
  @span("update_clone_memory")
  async def update_clone_memory(self, ...):
      ...
"""
from __future__ import annotations

import functools
import logging
import os
from contextlib import contextmanager
from typing import Any, Callable

logger = logging.getLogger("tracing")

# soft import: opentelemetry が無くても crash しない
try:
    from opentelemetry import trace as _otel_trace
    from opentelemetry.sdk.trace import TracerProvider
    from opentelemetry.sdk.trace.export import (
        BatchSpanProcessor,
        ConsoleSpanExporter,
        SimpleSpanProcessor,
    )
    from opentelemetry.sdk.resources import Resource, SERVICE_NAME
    _OTEL_AVAILABLE = True
except Exception as _e:
    _OTEL_AVAILABLE = False
    _otel_trace = None  # type: ignore
    logger.info(f"opentelemetry not available, tracing disabled: {_e}")


# OTLP exporter は別の optional package
try:
    from opentelemetry.exporter.otlp.proto.grpc.trace_exporter import (  # type: ignore
        OTLPSpanExporter,
    )
    _OTLP_AVAILABLE = True
except Exception:
    _OTLP_AVAILABLE = False
    OTLPSpanExporter = None  # type: ignore


_INITIALIZED = False
_TRACER = None


def init_tracing(service_name: str = "personal-brain") -> bool:
    """SDK 初期化。1 度だけ呼ぶ (idempotent)。

    Returns True if tracing is enabled (opentelemetry 入ってる + exporter 接続済 or console)。
    """
    global _INITIALIZED, _TRACER
    if _INITIALIZED:
        return _TRACER is not None
    _INITIALIZED = True

    if not _OTEL_AVAILABLE:
        logger.info("tracing disabled: opentelemetry not installed")
        return False

    resource = Resource.create({SERVICE_NAME: service_name})
    provider = TracerProvider(resource=resource)

    # exporter 選択
    otlp_endpoint = os.getenv("OTEL_EXPORTER_OTLP_ENDPOINT", "").strip()
    console_export = os.getenv("OTEL_CONSOLE", "").strip() in ("1", "true", "yes")

    exporter_count = 0
    if otlp_endpoint and _OTLP_AVAILABLE:
        try:
            otlp = OTLPSpanExporter(endpoint=otlp_endpoint)
            provider.add_span_processor(BatchSpanProcessor(otlp))
            logger.info(f"tracing → OTLP {otlp_endpoint}")
            exporter_count += 1
        except Exception as e:
            logger.warning(f"OTLP exporter setup failed: {e}")
    if console_export:
        provider.add_span_processor(SimpleSpanProcessor(ConsoleSpanExporter()))
        logger.info("tracing → console (stdout)")
        exporter_count += 1

    if exporter_count == 0:
        # no exporter — tracer は作るが span は捨てられる
        logger.info("tracing initialized but no exporter (OTLP / console とも未設定)")

    _otel_trace.set_tracer_provider(provider)
    _TRACER = _otel_trace.get_tracer(service_name)
    return True


def get_tracer():
    """tracer を返す (init 未済なら自動 init)。opentelemetry なしなら None。"""
    if not _INITIALIZED:
        init_tracing()
    return _TRACER


@contextmanager
def span(name: str, **attributes: Any):
    """span を作る context manager。

    opentelemetry が無ければ no-op (yield None)。
    attribute は span に attach される。例外時は record_exception + status=ERROR。

    使い方:
      with span("retrieval", query_chars=len(q)) as s:
          if s is not None:
              s.set_attribute("intent", intent)
          ...
    """
    tracer = get_tracer()
    if tracer is None:
        # no-op
        yield None
        return
    with tracer.start_as_current_span(name) as s:
        # attribute を attach
        for k, v in attributes.items():
            try:
                # OTel attribute は str/int/float/bool/list のみ受け付ける
                if isinstance(v, (str, int, float, bool)):
                    s.set_attribute(k, v)
                else:
                    s.set_attribute(k, str(v)[:200])
            except Exception:
                pass
        try:
            yield s
        except Exception as e:
            try:
                s.record_exception(e)
                from opentelemetry.trace import Status, StatusCode
                s.set_status(Status(StatusCode.ERROR, str(e)[:200]))
            except Exception:
                pass
            raise


def traced(name: str | None = None, **default_attrs: Any) -> Callable:
    """decorator として使えるラッパー。async / sync 両対応。

    @traced("update_clone_memory", layer="memory")
    async def update_clone_memory(self, ...):
        ...
    """
    import asyncio

    def deco(fn: Callable) -> Callable:
        span_name = name or fn.__qualname__
        if asyncio.iscoroutinefunction(fn):
            @functools.wraps(fn)
            async def async_wrapper(*args, **kwargs):
                with span(span_name, **default_attrs):
                    return await fn(*args, **kwargs)
            return async_wrapper
        else:
            @functools.wraps(fn)
            def sync_wrapper(*args, **kwargs):
                with span(span_name, **default_attrs):
                    return fn(*args, **kwargs)
            return sync_wrapper

    return deco


# ─── 状態取得 (test 用) ─────────────────────────
def is_enabled() -> bool:
    return _OTEL_AVAILABLE and _TRACER is not None


def reset_for_test() -> None:
    """test 用 reset。本番では呼ばない。"""
    global _INITIALIZED, _TRACER
    _INITIALIZED = False
    _TRACER = None
