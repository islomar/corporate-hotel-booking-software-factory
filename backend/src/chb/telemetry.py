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
"""

import asyncio
import logging
import math
import os
import time
import uuid
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from urllib.parse import urlparse

import requests
from fastapi import FastAPI
from opentelemetry.instrumentation.fastapi import FastAPIInstrumentor
from opentelemetry.sdk._logs import LoggerProvider, LoggingHandler
from opentelemetry.sdk._logs.export import BatchLogRecordProcessor, LogRecordExporter
from opentelemetry.sdk.metrics import MeterProvider
from opentelemetry.sdk.metrics.export import MetricReader, PeriodicExportingMetricReader
from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import BatchSpanProcessor, SpanExporter
from starlette.types import ASGIApp, Message, Receive, Scope, Send

# Stable HTTP semantic conventions: `http.server.request.duration` with `http.route`.
# The old default labels metrics with the raw path, one time series per URL.
# Read once, when the first app is instrumented.
os.environ.setdefault("OTEL_SEMCONV_STABILITY_OPT_IN", "http")

SERVICE_NAME = "chb"
# Upper bound on the latency a flush adds to one response.
FLUSH_TIMEOUT_MILLIS = 2_000
DEFAULT_METRIC_INTERVAL_SECONDS = 60.0


@dataclass
class Telemetry:
    tracer_provider: TracerProvider
    meter_provider: MeterProvider
    logger_provider: LoggerProvider | None = None
    metric_interval_seconds: float = DEFAULT_METRIC_INTERVAL_SECONDS
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
        tracer_provider = TracerProvider(resource=resource)
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
            app, tracer_provider=self.tracer_provider, meter_provider=self.meter_provider
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
        self.tracer_provider.force_flush(FLUSH_TIMEOUT_MILLIS)
        if self.logger_provider is not None:
            self.logger_provider.force_flush(FLUSH_TIMEOUT_MILLIS)
        now = self.clock()
        if now - self._last_metric_flush >= self.metric_interval_seconds:
            self._last_metric_flush = now
            self.meter_provider.force_flush(FLUSH_TIMEOUT_MILLIS)

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
            await asyncio.to_thread(self.telemetry.flush)
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
    metric_reader = PeriodicExportingMetricReader(
        OTLPMetricExporter(session=session), export_interval_millis=math.inf
    )
    log_exporter = None
    if env.get("OTEL_LOGS_EXPORTER") == "otlp":
        log_exporter = OTLPLogExporter(session=session)
    return Telemetry.create(
        span_exporter=OTLPSpanExporter(session=session),
        metric_reader=metric_reader,
        version=version,
        log_exporter=log_exporter,
        project_id=env.get("GOOGLE_CLOUD_PROJECT"),
        metric_interval_seconds=interval_millis / 1000,
    )


def project_id_from_env() -> str | None:
    return os.environ.get("GOOGLE_CLOUD_PROJECT") or None
