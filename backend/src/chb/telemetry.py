"""OpenTelemetry traces and metrics for the backend (ADR-c17d).

Environment variables choose where telemetry goes. The code is the same everywhere:

- No `OTEL_EXPORTER_OTLP_ENDPOINT` (tests, CI): no providers, no middleware, nothing exported.
- `OTEL_EXPORTER_OTLP_ENDPOINT=http://lgtm:4318` (`make dev`): OTLP/HTTP to the local Grafana.
- `OTEL_EXPORTER_OTLP_ENDPOINT=https://telemetry.googleapis.com` (Cloud Run): OTLP/HTTP to
  Google's Telemetry API, authenticated with Application Default Credentials.

Cloud Run throttles CPU once a response is sent, so background export threads may never run.
`FlushTelemetry` holds back the last chunk of each response until the request's spans are
exported. Metrics are flushed from the same place, at most once per export interval, because
Cloud Monitoring rejects points written too often to one time series.

Telemetry fails open: a slow or unreachable endpoint delays a response by at most
`flush_timeout_seconds`, and after such a flush the middleware stops flushing for
`breaker_seconds`. Spans then wait in the batch queue (dropped past 2048).
"""

import asyncio
import logging
import math
import os
import time
import uuid
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from urllib.parse import urlparse

import requests
from fastapi import FastAPI
from opentelemetry.context import Context
from opentelemetry.instrumentation.fastapi import FastAPIInstrumentor
from opentelemetry.sdk._logs import LoggerProvider, LoggingHandler
from opentelemetry.sdk._logs.export import BatchLogRecordProcessor, LogRecordExporter
from opentelemetry.sdk.metrics import MeterProvider
from opentelemetry.sdk.metrics.export import MetricReader, PeriodicExportingMetricReader
from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import BatchSpanProcessor, SpanExporter
from opentelemetry.sdk.trace.sampling import (
    ALWAYS_ON,
    Decision,
    ParentBased,
    Sampler,
    SamplingResult,
)
from opentelemetry.trace import Link, SpanKind, TraceState
from opentelemetry.util.types import Attributes
from starlette.types import ASGIApp, Message, Receive, Scope, Send

# Stable HTTP semantic conventions: `http.server.request.duration` with `http.route`.
# The old default labels metrics with the raw path, one time series per URL.
# Read once, when the first app is instrumented.
os.environ.setdefault("OTEL_SEMCONV_STABILITY_OPT_IN", "http")

SERVICE_NAME = "chb"
# Upper bound on the latency a flush adds to one response.
FLUSH_TIMEOUT_SECONDS = 2.0
# Per OTLP request, retries included. Below the flush timeout, so one export can finish.
EXPORT_TIMEOUT_SECONDS = 1.0
# A healthy export takes milliseconds. A flush slower than this, or one that fails, stops
# flushing for BREAKER_SECONDS: an endpoint that refuses connections makes the exporter retry
# until its timeout on every request.
SLOW_FLUSH_SECONDS = 0.5
BREAKER_SECONDS = 30.0
DEFAULT_METRIC_INTERVAL_SECONDS = 60.0
HEALTH_PATH = "/api/health"

_logger = logging.getLogger(__name__)


class DropHealthChecks(Sampler):
    """No traces for health checks. Their request metrics are still recorded."""

    def should_sample(
        self,
        parent_context: Context | None,
        trace_id: int,
        name: str,
        kind: SpanKind | None = None,
        attributes: Attributes = None,
        links: Sequence[Link] | None = None,
        trace_state: TraceState | None = None,
    ) -> SamplingResult:
        if attributes is not None and attributes.get("url.path") == HEALTH_PATH:
            return SamplingResult(Decision.DROP)
        return ALWAYS_ON.should_sample(
            parent_context, trace_id, name, kind, attributes, links, trace_state
        )

    def get_description(self) -> str:
        return "DropHealthChecks"


@dataclass
class Telemetry:
    tracer_provider: TracerProvider
    meter_provider: MeterProvider
    logger_provider: LoggerProvider | None = None
    metric_interval_seconds: float = DEFAULT_METRIC_INTERVAL_SECONDS
    flush_timeout_seconds: float = FLUSH_TIMEOUT_SECONDS
    slow_flush_seconds: float = SLOW_FLUSH_SECONDS
    breaker_seconds: float = BREAKER_SECONDS
    clock: Callable[[], float] = time.monotonic
    _last_metric_flush: float = field(default=-math.inf, init=False)

    @classmethod
    def create(
        cls,
        *,
        span_exporter: SpanExporter,
        metric_reader: MetricReader,
        version: str,
        log_exporter: LogRecordExporter | None = None,
        project_id: str | None = None,
        metric_interval_seconds: float = DEFAULT_METRIC_INTERVAL_SECONDS,
    ) -> Telemetry:
        attributes: dict[str, str] = {
            "service.name": SERVICE_NAME,
            "service.version": version,
            # Unique per process: two revisions running at once must not write the same series.
            "service.instance.id": str(uuid.uuid4()),
        }
        if project_id:
            attributes["gcp.project_id"] = project_id
        resource = Resource.create(attributes)
        tracer_provider = TracerProvider(resource=resource, sampler=ParentBased(DropHealthChecks()))
        tracer_provider.add_span_processor(BatchSpanProcessor(span_exporter))
        logger_provider = None
        if log_exporter is not None:
            logger_provider = LoggerProvider(resource=resource)
            logger_provider.add_log_record_processor(BatchLogRecordProcessor(log_exporter))
        return cls(
            tracer_provider=tracer_provider,
            meter_provider=MeterProvider(resource=resource, metric_readers=[metric_reader]),
            logger_provider=logger_provider,
            metric_interval_seconds=metric_interval_seconds,
        )

    def instrument(self, app: FastAPI) -> None:
        FastAPIInstrumentor.instrument_app(
            app,
            tracer_provider=self.tracer_provider,
            meter_provider=self.meter_provider,
            # One span per request, without a child span per ASGI message.
            exclude_spans=["receive", "send"],
        )
        # Wrap the instrumented stack, so the flush runs after the server span has ended.
        build_instrumented_stack = app.build_middleware_stack

        def build_middleware_stack() -> ASGIApp:
            return FlushTelemetry(build_instrumented_stack(), self)

        app.build_middleware_stack = build_middleware_stack  # type: ignore[method-assign]

    def log_handler(self) -> logging.Handler | None:
        if self.logger_provider is None:
            return None
        return LoggingHandler(logger_provider=self.logger_provider)

    def flush(self) -> None:
        """Blocking export. The SDK ignores the timeout argument: callers bound the wait."""
        timeout_millis = int(self.flush_timeout_seconds * 1000)
        self.tracer_provider.force_flush(timeout_millis)
        if self.logger_provider is not None:
            self.logger_provider.force_flush(timeout_millis)
        now = self.clock()
        if now - self._last_metric_flush >= self.metric_interval_seconds:
            self._last_metric_flush = now
            self.meter_provider.force_flush(timeout_millis)

    def shutdown(self) -> None:
        self.tracer_provider.shutdown()
        self.meter_provider.shutdown()
        if self.logger_provider is not None:
            self.logger_provider.shutdown()


class FlushTelemetry:
    """Holds the final response chunk until telemetry is flushed. Cost: one export round trip."""

    def __init__(self, app: ASGIApp, telemetry: Telemetry) -> None:
        self.app = app
        self.telemetry = telemetry
        self._skip_until = -math.inf

    async def _flush(self) -> None:
        telemetry = self.telemetry
        started = telemetry.clock()
        if started < self._skip_until:
            return
        try:
            async with asyncio.timeout(telemetry.flush_timeout_seconds):
                await asyncio.to_thread(telemetry.flush)
        except Exception as error:
            # Includes TimeoutError. The export thread may still run: it ends at its own timeout.
            self._open_breaker(started, f"failed: {error!r}")
            return
        elapsed = telemetry.clock() - started
        if elapsed > telemetry.slow_flush_seconds:
            self._open_breaker(started, f"took {elapsed:.2f} s")

    def _open_breaker(self, started: float, reason: str) -> None:
        self._skip_until = started + self.telemetry.breaker_seconds
        _logger.warning(
            "Telemetry flush %s; responses skip flushing for %.0f s",
            reason,
            self.telemetry.breaker_seconds,
        )

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        held: list[Message] = []

        async def holding_send(message: Message) -> None:
            if message["type"] == "http.response.body" and not message.get("more_body", False):
                held.append(message)
                return
            await send(message)

        try:
            await self.app(scope, receive, holding_send)
        finally:
            await self._flush()
            for message in held:
                await send(message)


def uses_google_auth(endpoint: str) -> bool:
    host = urlparse(endpoint).hostname or ""
    return host == "googleapis.com" or host.endswith(".googleapis.com")


def _google_session() -> requests.Session:
    """A requests session that adds and refreshes Application Default Credentials."""
    import google.auth  # only Cloud Run needs it
    from google.auth.transport.requests import AuthorizedSession

    credentials, _ = google.auth.default(  # pyright: ignore[reportUnknownMemberType, reportUnknownVariableType]
        scopes=["https://www.googleapis.com/auth/cloud-platform"]
    )
    return AuthorizedSession(credentials)  # pyright: ignore[reportUnknownArgumentType]


def telemetry_from_env(env: Mapping[str, str], version: str) -> Telemetry | None:
    endpoint = env.get("OTEL_EXPORTER_OTLP_ENDPOINT", "")
    if not endpoint or env.get("OTEL_SDK_DISABLED", "").lower() == "true":
        return None

    from opentelemetry.exporter.otlp.proto.http._log_exporter import (
        OTLPLogExporter,
    )
    from opentelemetry.exporter.otlp.proto.http.metric_exporter import (
        OTLPMetricExporter,
    )
    from opentelemetry.exporter.otlp.proto.http.trace_exporter import (
        OTLPSpanExporter,
    )

    session = _google_session() if uses_google_auth(endpoint) else None
    interval_millis = float(env.get("OTEL_METRIC_EXPORT_INTERVAL", "60000"))
    # No timer thread: FlushTelemetry and shutdown export metrics (see the module docstring).
    timeout = EXPORT_TIMEOUT_SECONDS
    metric_reader = PeriodicExportingMetricReader(
        OTLPMetricExporter(session=session, timeout=timeout), export_interval_millis=math.inf
    )
    log_exporter = None
    if env.get("OTEL_LOGS_EXPORTER") == "otlp":
        log_exporter = OTLPLogExporter(session=session, timeout=timeout)
    return Telemetry.create(
        span_exporter=OTLPSpanExporter(session=session, timeout=timeout),
        metric_reader=metric_reader,
        version=version,
        log_exporter=log_exporter,
        project_id=env.get("GOOGLE_CLOUD_PROJECT"),
        metric_interval_seconds=interval_millis / 1000,
    )
