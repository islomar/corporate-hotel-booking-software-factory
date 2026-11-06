import asyncio
import io
import json
import logging
import math
import threading
import time
from collections.abc import Iterator, Sequence
from dataclasses import dataclass, field

import httpx2
import pytest
from fastapi.testclient import TestClient
from opentelemetry.sdk.metrics.export import (
    InMemoryMetricReader,
    MetricExporter,
    MetricExportResult,
    MetricsData,
    PeriodicExportingMetricReader,
)
from opentelemetry.sdk.trace import ReadableSpan
from opentelemetry.sdk.trace.export import SpanExportResult
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

from chb.app import app_from_env, create_app
from chb.logs import JsonFormatter, configure_logging
from chb.telemetry import FlushTelemetry, Telemetry, telemetry_from_env, uses_google_auth


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
    client = TestClient(telemetry.wrap(create_app(telemetry=telemetry)))

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

    TestClient(telemetry.wrap(app)).get("/api/log-something")

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
    client = TestClient(telemetry.wrap(create_app(telemetry=telemetry)))

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
    app = telemetry.wrap(create_app(telemetry=telemetry))

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
    TestClient(telemetry.wrap(create_app(telemetry=telemetry))).get("/api/health")

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


@pytest.fixture
def restore_logging() -> Iterator[None]:
    names = ["", "uvicorn", "uvicorn.error", "uvicorn.access"]
    saved = [
        (logging.getLogger(n), logging.getLogger(n).handlers[:], logging.getLogger(n).propagate)
        for n in names
    ]
    level = logging.getLogger().level
    yield
    for logger, handlers, propagate in saved:
        logger.handlers = handlers
        logger.propagate = propagate
    logging.getLogger().setLevel(level)


@pytest.mark.usefixtures("restore_logging")
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


@dataclass
class BlockingTelemetry(Telemetry):
    """A flush that hangs, like an export to an endpoint that does not answer."""

    flushes: int = 0
    release: threading.Event = field(default_factory=threading.Event)

    def flush(self) -> float:
        self.flushes += 1
        self.release.wait(5)
        return 5.0


def test_hanging_endpoint_neither_blocks_responses_nor_piles_up_flushes(
    spans: InMemorySpanExporter, metrics: InMemoryMetricReader
) -> None:
    created = Telemetry.create(span_exporter=spans, metric_reader=metrics, version="dev")
    telemetry = BlockingTelemetry(
        tracer_provider=created.tracer_provider,
        meter_provider=created.meter_provider,
        flush_timeout_seconds=0.2,
    )
    app = telemetry.wrap(create_app(telemetry=telemetry))

    async def get() -> int:
        transport = httpx2.ASGITransport(app=app)
        async with httpx2.AsyncClient(transport=transport, base_url="http://test") as client:
            return (await client.get("/api/openapi.json")).status_code

    async def scenario() -> tuple[list[int], list[int], int, bool]:
        # One event loop, as under uvicorn: the hung flush thread keeps running.
        try:
            started = time.monotonic()
            concurrent = await asyncio.gather(*(get() for _ in range(10)))
            seconds = time.monotonic() - started
            after_breaker = [await get()]
            flushes = telemetry.flushes
            return concurrent, after_breaker, flushes, seconds < 3
        finally:
            telemetry.release.set()

    concurrent, after_breaker, flushes, within_bound = asyncio.run(scenario())

    assert concurrent == [200] * 10
    assert after_breaker == [200]
    # Ten responses shared one flush thread: no thread per request.
    assert flushes == 1
    assert app.breaker_open
    # The timeout is 0.2 s. A generous bound: the responses did not wait for the hung flush.
    assert within_bound


def test_slow_flush_opens_the_breaker_until_it_expires(
    spans: InMemorySpanExporter, metrics: InMemoryMetricReader
) -> None:
    now = [0.0]
    flushes: list[float] = []

    class SlowTelemetry(Telemetry):
        def flush(self) -> float:
            flushes.append(now[0])
            now[0] += 1.0
            return 1.0  # each span flush "takes" one second

    created = Telemetry.create(span_exporter=spans, metric_reader=metrics, version="dev")
    telemetry = SlowTelemetry(
        tracer_provider=created.tracer_provider,
        meter_provider=created.meter_provider,
        breaker_seconds=30,
    )
    telemetry.clock = lambda: now[0]
    client = TestClient(telemetry.wrap(create_app(telemetry=telemetry)))

    for t in (0.0, 10.0, 29.0, 31.0):
        now[0] = t
        client.get("/api/openapi.json")

    assert flushes == [0.0, 31.0]


class FailingExporter(InMemorySpanExporter):
    """An endpoint that answers fast with an error, like a 403 before IAM propagates."""

    def __init__(self) -> None:
        super().__init__()
        self.calls = 0

    def export(self, spans: Sequence[ReadableSpan]) -> SpanExportResult:
        self.calls += 1
        return SpanExportResult.FAILURE


def test_fast_export_failure_opens_the_breaker(metrics: InMemoryMetricReader) -> None:
    exporter = FailingExporter()
    telemetry = Telemetry.create(span_exporter=exporter, metric_reader=metrics, version="dev")
    client = TestClient(telemetry.wrap(create_app(telemetry=telemetry)))

    for _ in range(3):
        assert client.get("/api/openapi.json").status_code == 200

    assert exporter.calls == 1


def test_shutdown_exports_metrics_not_yet_exported(spans: InMemorySpanExporter) -> None:
    exported: list[MetricsData] = []

    class RecordingExporter(MetricExporter):
        def __init__(self) -> None:
            super().__init__()

        def export(
            self, metrics_data: MetricsData, timeout_millis: float = 10_000, **kwargs: object
        ) -> MetricExportResult:
            exported.append(metrics_data)
            return MetricExportResult.SUCCESS

        def force_flush(self, timeout_millis: float = 10_000) -> bool:
            return True

        def shutdown(self, timeout_millis: float = 30_000, **kwargs: object) -> None:
            pass

    reader = PeriodicExportingMetricReader(RecordingExporter(), export_interval_millis=math.inf)
    telemetry = Telemetry.create(span_exporter=spans, metric_reader=reader, version="dev")
    telemetry.meter_provider.get_meter("t").create_counter("bookings").add(1)

    telemetry.shutdown()

    assert len(exported) == 1


def _traceparent(sampled: bool) -> str:
    return f"00-{'ab' * 16}-{'cd' * 8}-{'01' if sampled else '00'}"


@pytest.mark.parametrize("sampled", [True, False])
def test_local_rules_decide_sampling_whatever_the_caller_says(
    telemetry: Telemetry, spans: InMemorySpanExporter, sampled: bool
) -> None:
    client = TestClient(telemetry.wrap(create_app(telemetry=telemetry)))
    headers = {"traceparent": _traceparent(sampled)}

    client.get("/api/openapi.json", headers=headers)
    client.get("/api/health", headers=headers)

    [server_span] = spans.get_finished_spans()
    assert server_span.name == "GET /api/openapi.json"
    assert server_span.context is not None
    # Same trace as the caller, so Cloud Trace joins it with the Cloud Run frontend spans.
    assert format(server_span.context.trace_id, "032x") == "ab" * 16


@pytest.mark.usefixtures("restore_logging")
def test_production_entry_point_flushes_at_the_asgi_edge() -> None:
    app = app_from_env({"OTEL_EXPORTER_OTLP_ENDPOINT": "http://127.0.0.1:1"})

    try:
        assert isinstance(app, FlushTelemetry)
    finally:
        if isinstance(app, FlushTelemetry):
            app.telemetry.shutdown()


def test_shutdown_waits_for_a_running_flush(
    spans: InMemorySpanExporter, metrics: InMemoryMetricReader
) -> None:
    events: list[str] = []
    flush_started = threading.Event()

    class OrderedTelemetry(Telemetry):
        def flush(self) -> float:
            flush_started.set()
            time.sleep(0.1)
            events.append("flush")
            return 0.1

        def _shutdown_providers(self) -> None:
            events.append("shutdown")

    created = Telemetry.create(span_exporter=spans, metric_reader=metrics, version="dev")
    telemetry = OrderedTelemetry(
        tracer_provider=created.tracer_provider, meter_provider=created.meter_provider
    )

    telemetry.request_flush()
    flush_started.wait(5)
    telemetry.shutdown()

    assert events == ["flush", "shutdown"]


@pytest.mark.usefixtures("restore_logging")
def test_uvicorn_logs_and_exceptions_become_json_lines(
    capsys: pytest.CaptureFixture[str],
) -> None:
    configure_logging("my-project", [])

    logging.getLogger("uvicorn.error").info("Started server process")
    try:
        raise ValueError("bad booking")
    except ValueError:
        logging.getLogger("chb.test").exception("request failed")

    started, failed = (json.loads(line) for line in capsys.readouterr().out.splitlines())
    assert started["logger"] == "uvicorn.error"
    assert started["message"] == "Started server process"
    assert failed["severity"] == "ERROR"
    assert failed["message"].startswith("request failed\nTraceback")
    assert "ValueError: bad booking" in failed["message"]


CLOUD_RUN_ENV = {
    "OTEL_EXPORTER_OTLP_ENDPOINT": "https://telemetry.googleapis.com",
    "GOOGLE_CLOUD_PROJECT": "my-project",
    "GOOGLE_CLOUD_REGION": "europe-west1",
    # Cloud Run sets these two in every container.
    "K_SERVICE": "chb",
    "K_REVISION": "chb-00042-abc",
}


@pytest.fixture
def no_google_credentials(monkeypatch: pytest.MonkeyPatch) -> None:
    import requests

    monkeypatch.setattr("chb.telemetry._google_session", requests.Session)


@pytest.mark.usefixtures("no_google_credentials")
def test_cloud_run_resource_has_the_location_cloud_monitoring_requires() -> None:
    # telemetry.googleapis.com writes metrics to the `prometheus_target` monitored resource.
    # Its `location` label comes from `cloud.region`; without it every point is rejected with
    # 400 "Unrecognized region or location" (CHB-103).
    telemetry = telemetry_from_env(CLOUD_RUN_ENV, version="abc123")

    assert telemetry is not None
    try:
        attributes = telemetry.tracer_provider.resource.attributes
        assert attributes["cloud.region"] == "europe-west1"
        assert attributes["cloud.provider"] == "gcp"
        assert attributes["cloud.platform"] == "gcp_cloud_run"
        assert attributes["faas.name"] == "chb"
        assert attributes["faas.version"] == "chb-00042-abc"
        assert telemetry.metrics_off_reason is None
    finally:
        telemetry.shutdown()


@pytest.mark.usefixtures("no_google_credentials", "restore_logging")
def test_google_endpoint_without_region_turns_metrics_off_with_one_warning(
    capsys: pytest.CaptureFixture[str],
) -> None:
    env = {k: v for k, v in CLOUD_RUN_ENV.items() if k != "GOOGLE_CLOUD_REGION"}

    app = app_from_env(env)

    assert isinstance(app, FlushTelemetry)
    try:
        assert app.telemetry.metrics_off_reason is not None
        assert TestClient(app).get("/api/health").status_code == 200
        warnings = [
            json.loads(line) for line in capsys.readouterr().out.splitlines() if '"WARNING"' in line
        ]
        assert len(warnings) == 1
        assert "GOOGLE_CLOUD_REGION" in warnings[0]["message"]
    finally:
        app.telemetry.shutdown()


def test_failing_metric_export_does_not_stop_trace_flushing(spans: InMemorySpanExporter) -> None:
    now = [0.0]

    class SlowRejectingExporter(MetricExporter):
        """Like telemetry.googleapis.com answering 400 after most of a second."""

        def __init__(self) -> None:
            super().__init__()
            self.calls = 0

        def export(
            self, metrics_data: MetricsData, timeout_millis: float = 10_000, **kwargs: object
        ) -> MetricExportResult:
            self.calls += 1
            now[0] += 0.9
            return MetricExportResult.FAILURE

        def force_flush(self, timeout_millis: float = 10_000) -> bool:
            return True

        def shutdown(self, timeout_millis: float = 30_000, **kwargs: object) -> None:
            pass

    exporter = SlowRejectingExporter()
    reader = PeriodicExportingMetricReader(exporter, export_interval_millis=math.inf)
    telemetry = Telemetry.create(
        span_exporter=spans, metric_reader=reader, version="dev", metric_interval_seconds=60
    )
    telemetry.clock = lambda: now[0]
    app = telemetry.wrap(create_app(telemetry=telemetry))
    client = TestClient(app)

    client.get("/api/openapi.json")
    now[0] = 5.0
    client.get("/api/openapi.json")

    assert exporter.calls == 1
    assert not app.breaker_open
    # Both requests' spans were exported before their responses: no breaker skipped a flush.
    assert len(spans.get_finished_spans()) == 2


@pytest.mark.parametrize(
    ("shutdown_at", "expected"), [(3.0, [0.0]), (10.0, [0.0, 10.0])], ids=["3 s", "10 s"]
)
def test_shutdown_flushes_metrics_unless_written_moments_ago(
    spans: InMemorySpanExporter, shutdown_at: float, expected: list[float]
) -> None:
    # Cloud Monitoring rejects a second point within 5 s on one time series (400
    # FAILED_PRECONDITION "written more frequently than the maximum sampling period").
    now = [0.0]
    exports: list[float] = []

    class CountingReader(InMemoryMetricReader):
        def force_flush(self, timeout_millis: float = 10_000) -> bool:
            exports.append(now[0])
            return True

    telemetry = Telemetry.create(span_exporter=spans, metric_reader=CountingReader(), version="dev")
    telemetry.clock = lambda: now[0]
    telemetry.flush()
    now[0] = shutdown_at

    telemetry.shutdown()

    assert exports == expected
