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
One thread runs flushes, one at a time. Concurrent responses share the next pending flush,
so a hanging endpoint never piles up threads.
"""

import asyncio
import logging
import math
import os
import threading
import time
import uuid
from collections.abc import Callable, Mapping, Sequence
from concurrent.futures import Future, ThreadPoolExecutor
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
from opentelemetry.sdk.trace import ReadableSpan, TracerProvider
from opentelemetry.sdk.trace.export import BatchSpanProcessor, SpanExporter, SpanExportResult
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
    """No traces for health checks. Their request metrics are still recorded.

    Also used for remote parents: the caller's sampled flag in `traceparent` does not decide.
    The span keeps the caller's trace ID.
    """

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


class ExportFailedError(Exception):
    pass


class FailureTrackingSpanExporter(SpanExporter):
    """Remembers failed exports. `force_flush` reports success even when the export failed."""

    def __init__(self, exporter: SpanExporter) -> None:
        self._exporter = exporter
        self._failed = False
        self._lock = threading.Lock()

    def export(self, spans: Sequence[ReadableSpan]) -> SpanExportResult:
        result = self._exporter.export(spans)
        if result is not SpanExportResult.SUCCESS:
            with self._lock:
                self._failed = True
        return result

    def take_failure(self) -> bool:
        with self._lock:
            failed, self._failed = self._failed, False
        return failed

    def shutdown(self) -> None:
        self._exporter.shutdown()

    def force_flush(self, timeout_millis: int = 30_000) -> bool:
        return self._exporter.force_flush(timeout_millis)


@dataclass
class Telemetry:
    tracer_provider: TracerProvider
    meter_provider: MeterProvider
    logger_provider: LoggerProvider | None = None
    span_exports: FailureTrackingSpanExporter | None = None
    metric_interval_seconds: float = DEFAULT_METRIC_INTERVAL_SECONDS
    flush_timeout_seconds: float = FLUSH_TIMEOUT_SECONDS
    slow_flush_seconds: float = SLOW_FLUSH_SECONDS
    breaker_seconds: float = BREAKER_SECONDS
    clock: Callable[[], float] = time.monotonic
    _last_metric_flush: float = field(default=-math.inf, init=False)
    # One thread runs every flush and the final shutdown, so they never overlap.
    _executor: ThreadPoolExecutor = field(
        default_factory=lambda: ThreadPoolExecutor(max_workers=1, thread_name_prefix="otel-flush"),
        init=False,
    )
    _pending: Future[None] | None = field(default=None, init=False)
    _pending_lock: threading.Lock = field(default_factory=threading.Lock, init=False)

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
        local_rules = DropHealthChecks()
        sampler = ParentBased(
            root=local_rules,
            remote_parent_sampled=local_rules,
            remote_parent_not_sampled=local_rules,
        )
        tracer_provider = TracerProvider(resource=resource, sampler=sampler)
        span_exports = FailureTrackingSpanExporter(span_exporter)
        tracer_provider.add_span_processor(BatchSpanProcessor(span_exports))
        logger_provider = None
        if log_exporter is not None:
            logger_provider = LoggerProvider(resource=resource)
            logger_provider.add_log_record_processor(BatchLogRecordProcessor(log_exporter))
        return cls(
            tracer_provider=tracer_provider,
            meter_provider=MeterProvider(resource=resource, metric_readers=[metric_reader]),
            logger_provider=logger_provider,
            span_exports=span_exports,
            metric_interval_seconds=metric_interval_seconds,
        )

    def instrument(self, app: FastAPI) -> None:
        # Stable HTTP semantic conventions: `http.server.request.duration` with `http.route`.
        # The old default labels metrics with the raw path, one time series per URL.
        # The instrumentation reads this variable once per process, at its first use.
        os.environ.setdefault("OTEL_SEMCONV_STABILITY_OPT_IN", "http")
        FastAPIInstrumentor.instrument_app(
            app,
            tracer_provider=self.tracer_provider,
            meter_provider=self.meter_provider,
            # One span per request, without a child span per ASGI message.
            exclude_spans=["receive", "send"],
        )

    def wrap(self, app: ASGIApp) -> FlushTelemetry:
        """The ASGI entry point: outside the app, so the server span has ended at flush time."""
        return FlushTelemetry(app, self)

    def log_handler(self) -> logging.Handler | None:
        if self.logger_provider is None:
            return None
        return LoggingHandler(logger_provider=self.logger_provider)

    def request_flush(self) -> Future[None]:
        """A flush that starts after this call. Joins the pending one if it has not started."""
        with self._pending_lock:
            pending = self._pending
            if pending is None or pending.running() or pending.done():
                pending = self._executor.submit(self.flush)
                self._pending = pending
            return pending

    def flush(self) -> None:
        """Blocking export. The SDK ignores the timeout argument: callers bound the wait.

        Not thread-safe: run it through `request_flush`, on the flush thread.
        """
        timeout_millis = int(self.flush_timeout_seconds * 1000)
        self.tracer_provider.force_flush(timeout_millis)
        if self.logger_provider is not None:
            self.logger_provider.force_flush(timeout_millis)
        now = self.clock()
        if now - self._last_metric_flush >= self.metric_interval_seconds:
            self._last_metric_flush = now
            self.meter_provider.force_flush(timeout_millis)
        if self.span_exports is not None and self.span_exports.take_failure():
            raise ExportFailedError("span export failed")

    def shutdown(self, timeout_seconds: float = 5.0) -> None:
        """Export what is left, on the flush thread, after any running flush."""
        try:
            self._executor.submit(self._shutdown_providers).result(timeout=timeout_seconds)
        except TimeoutError:
            # A flush hangs on the endpoint. The process is exiting: give up on the rest.
            _logger.warning("Telemetry shutdown timed out after %.0f s", timeout_seconds)
        finally:
            self._executor.shutdown(wait=False, cancel_futures=True)

    def _shutdown_providers(self) -> None:
        # The metric reader has no timer thread, so its shutdown would not collect: flush first.
        self.meter_provider.force_flush()
        self.tracer_provider.shutdown()
        self.meter_provider.shutdown()
        if self.logger_provider is not None:
            self.logger_provider.shutdown()


class FlushTelemetry:
    """Holds the final response chunk until telemetry is flushed. Cost: one export round trip."""

    def __init__(self, app: ASGIApp, telemetry: Telemetry) -> None:
        self.app = app
        self.telemetry = telemetry
        self._lock = threading.Lock()
        self._skip_until = -math.inf

    @property
    def breaker_open(self) -> bool:
        with self._lock:
            return self.telemetry.clock() < self._skip_until

    async def _flush(self) -> None:
        telemetry = self.telemetry
        if self.breaker_open:
            return
        started = telemetry.clock()
        try:
            async with asyncio.timeout(telemetry.flush_timeout_seconds):
                await asyncio.shield(asyncio.wrap_future(telemetry.request_flush()))
        except Exception as error:
            # Includes TimeoutError. The flush thread may still run: it ends at its own timeout.
            self._open_breaker(f"failed: {error!r}")
            return
        elapsed = telemetry.clock() - started
        if elapsed > telemetry.slow_flush_seconds:
            self._open_breaker(f"took {elapsed:.2f} s")

    def _open_breaker(self, reason: str) -> None:
        with self._lock:
            now = self.telemetry.clock()
            if now < self._skip_until:
                return  # another response opened it: one warning is enough
            self._skip_until = now + self.telemetry.breaker_seconds
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
