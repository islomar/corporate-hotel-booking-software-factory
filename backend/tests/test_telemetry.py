import asyncio
import io
import json
import logging
import threading
import time
from collections.abc import Iterator

import httpx2
import pytest
from fastapi.testclient import TestClient
from opentelemetry.sdk.metrics.export import InMemoryMetricReader
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

from chb.app import app_from_env, create_app
from chb.logs import JsonFormatter
from chb.telemetry import Telemetry, telemetry_from_env, uses_google_auth


@pytest.fixture
def spans() -> InMemorySpanExporter:
    return InMemorySpanExporter()


@pytest.fixture
def metrics() -> InMemoryMetricReader:
    return InMemoryMetricReader()


@pytest.fixture
def telemetry(spans: InMemorySpanExporter, metrics: InMemoryMetricReader) -> Telemetry:
    return Telemetry.create(span_exporter=spans, metric_reader=metrics, version="abc123")


@pytest.fixture
def log_lines() -> Iterator[io.StringIO]:
    stream = io.StringIO()
    handler = logging.StreamHandler(stream)
    handler.setFormatter(JsonFormatter(project_id="my-project"))
    logger = logging.getLogger("chb")
    logger.addHandler(handler)
    logger.setLevel(logging.INFO)
    yield stream
    logger.removeHandler(handler)


def test_request_produces_a_span_with_the_route(
    telemetry: Telemetry, spans: InMemorySpanExporter
) -> None:
    client = TestClient(create_app(telemetry=telemetry))

    client.get("/api/openapi.json")

    [server_span] = spans.get_finished_spans()
    assert server_span.name == "GET /api/openapi.json"
    assert server_span.attributes is not None
    assert server_span.attributes["http.route"] == "/api/openapi.json"
    assert server_span.resource.attributes["service.name"] == "chb"
    assert server_span.resource.attributes["service.version"] == "abc123"


def test_log_line_during_a_request_carries_the_trace_id(
    telemetry: Telemetry, spans: InMemorySpanExporter, log_lines: io.StringIO
) -> None:
    app = create_app(telemetry=telemetry)

    @app.get("/api/log-something")
    def log_something() -> None:  # pyright: ignore[reportUnusedFunction]
        logging.getLogger("chb.test").warning("hello")

    TestClient(app).get("/api/log-something")

    server_span = next(s for s in spans.get_finished_spans() if s.parent is None)
    assert server_span.context is not None
    trace_id = format(server_span.context.trace_id, "032x")
    line = json.loads(log_lines.getvalue().splitlines()[-1])
    assert line["message"] == "hello"
    assert line["severity"] == "WARNING"
    assert line["logging.googleapis.com/trace"] == f"projects/my-project/traces/{trace_id}"
    assert line["trace_id"] == trace_id
    assert len(line["logging.googleapis.com/spanId"]) == 16
    assert line["logging.googleapis.com/trace_sampled"] is True


def test_request_records_duration_metric_by_route(
    telemetry: Telemetry, metrics: InMemoryMetricReader
) -> None:
    client = TestClient(create_app(telemetry=telemetry))

    client.get("/api/health")

    data = metrics.get_metrics_data()
    assert data is not None
    points = [
        point
        for resource_metrics in data.resource_metrics
        for scope_metrics in resource_metrics.scope_metrics
        for metric in scope_metrics.metrics
        if metric.name == "http.server.request.duration"
        for point in metric.data.data_points
    ]
    assert any(p.attributes and p.attributes.get("http.route") == "/api/health" for p in points)


def test_spans_are_flushed_before_the_response_is_released(
    telemetry: Telemetry, spans: InMemorySpanExporter
) -> None:
    exported_when_body_sent: list[int] = []
    app = create_app(telemetry=telemetry)

    async def asgi(scope: dict[str, object], receive: object, send: object) -> None:
        async def recording_send(message: dict[str, object]) -> None:
            if message["type"] == "http.response.body" and not message.get("more_body"):
                exported_when_body_sent.append(len(spans.get_finished_spans()))
            await send(message)  # type: ignore[operator]

        await app(scope, receive, recording_send)  # type: ignore[arg-type]

    TestClient(asgi).get("/api/openapi.json")  # type: ignore[arg-type]

    assert exported_when_body_sent[0] >= 1


def test_health_checks_are_not_traced_but_still_counted(
    telemetry: Telemetry, spans: InMemorySpanExporter, metrics: InMemoryMetricReader
) -> None:
    TestClient(create_app(telemetry=telemetry)).get("/api/health")

    assert spans.get_finished_spans() == ()
    data = metrics.get_metrics_data()
    assert data is not None
    names = [
        metric.name
        for resource_metrics in data.resource_metrics
        for scope_metrics in resource_metrics.scope_metrics
        for metric in scope_metrics.metrics
    ]
    assert "http.server.request.duration" in names


def test_no_exporter_configured_means_no_telemetry() -> None:
    assert telemetry_from_env({}, version="dev") is None
    assert telemetry_from_env({"OTEL_SDK_DISABLED": "true"}, version="dev") is None


def test_log_line_outside_a_request_has_no_trace_fields(log_lines: io.StringIO) -> None:
    logging.getLogger("chb.test").info("startup")

    line = json.loads(log_lines.getvalue().splitlines()[-1])
    assert line == {"severity": "INFO", "message": "startup", "logger": "chb.test"} | {
        "time": line["time"]
    }


def test_log_line_without_project_keeps_plain_trace_id(
    telemetry: Telemetry, spans: InMemorySpanExporter
) -> None:
    stream = io.StringIO()
    handler = logging.StreamHandler(stream)
    handler.setFormatter(JsonFormatter(project_id=None))
    with telemetry.tracer_provider.get_tracer("t").start_as_current_span("s"):
        record = logging.LogRecord("chb", logging.ERROR, __file__, 1, "boom", None, None)
        handler.emit(record)

    line = json.loads(stream.getvalue())
    assert "logging.googleapis.com/trace" not in line
    assert len(line["trace_id"]) == 32


def test_metrics_are_flushed_at_most_once_per_interval(spans: InMemorySpanExporter) -> None:
    exports: list[float] = []
    now = [0.0]

    class CountingReader(InMemoryMetricReader):
        def force_flush(self, timeout_millis: float = 10_000) -> bool:
            exports.append(now[0])
            return True

    telemetry = Telemetry.create(
        span_exporter=spans,
        metric_reader=CountingReader(),
        version="dev",
        metric_interval_seconds=60,
    )
    telemetry.clock = lambda: now[0]

    for t in (0.0, 30.0, 59.0, 60.0, 61.0, 125.0):
        now[0] = t
        telemetry.flush()

    assert exports == [0.0, 60.0, 125.0]


def test_google_endpoints_use_application_default_credentials() -> None:
    assert uses_google_auth("https://telemetry.googleapis.com")
    assert not uses_google_auth("http://lgtm:4318")
    assert not uses_google_auth("https://evilgoogleapis.com")


def test_app_serves_when_telemetry_setup_fails(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    def no_credentials() -> None:
        raise RuntimeError("no Application Default Credentials")

    monkeypatch.setattr("chb.telemetry._google_session", no_credentials)

    app = app_from_env({"OTEL_EXPORTER_OTLP_ENDPOINT": "https://telemetry.googleapis.com"})

    assert TestClient(app).get("/api/health").json()["status"] == "ok"
    warnings = [
        json.loads(line) for line in capsys.readouterr().out.splitlines() if '"WARNING"' in line
    ]
    assert len(warnings) == 1
    assert "Telemetry is off" in warnings[0]["message"]
    assert "no Application Default Credentials" in warnings[0]["message"]


class BlockingTelemetry(Telemetry):
    """A flush that hangs, like an export to an endpoint that does not answer."""

    flushes = 0
    release = threading.Event()

    def flush(self) -> None:
        type(self).flushes += 1
        self.release.wait(5)


def test_hanging_endpoint_delays_a_response_by_at_most_the_timeout(
    spans: InMemorySpanExporter, metrics: InMemoryMetricReader
) -> None:
    created = Telemetry.create(span_exporter=spans, metric_reader=metrics, version="dev")
    telemetry = BlockingTelemetry(
        tracer_provider=created.tracer_provider,
        meter_provider=created.meter_provider,
        flush_timeout_seconds=0.2,
    )
    app = create_app(telemetry=telemetry)

    async def timed_get() -> tuple[int, float]:
        # Measured inside one event loop, as under uvicorn: the hung export thread keeps
        # running, but the response does not wait for it.
        transport = httpx2.ASGITransport(app=app)
        async with httpx2.AsyncClient(transport=transport, base_url="http://test") as client:
            started = time.monotonic()
            response = await client.get("/api/openapi.json")
            return response.status_code, time.monotonic() - started

    async def two_requests() -> list[tuple[int, float]]:
        try:
            return [await timed_get(), await timed_get()]
        finally:
            BlockingTelemetry.release.set()

    (first_status, first_seconds), (second_status, second_seconds) = asyncio.run(two_requests())

    assert first_status == second_status == 200
    assert 0.2 <= first_seconds < 1
    # The breaker is open: the second response does not wait for a flush.
    assert second_seconds < 0.1
    assert BlockingTelemetry.flushes == 1


def test_slow_flush_opens_the_breaker_until_it_expires(
    spans: InMemorySpanExporter, metrics: InMemoryMetricReader
) -> None:
    now = [0.0]
    flushes: list[float] = []

    class SlowTelemetry(Telemetry):
        def flush(self) -> None:
            flushes.append(now[0])
            now[0] += 1.0  # each flush "takes" one second

    created = Telemetry.create(span_exporter=spans, metric_reader=metrics, version="dev")
    telemetry = SlowTelemetry(
        tracer_provider=created.tracer_provider,
        meter_provider=created.meter_provider,
        breaker_seconds=30,
    )
    telemetry.clock = lambda: now[0]
    client = TestClient(create_app(telemetry=telemetry))

    for t in (0.0, 10.0, 29.0, 31.0):
        now[0] = t
        client.get("/api/openapi.json")

    assert flushes == [0.0, 31.0]
