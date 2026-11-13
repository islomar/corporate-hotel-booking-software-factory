"""Request size and rate limits (CHB-23).

The app has no write route yet, so the tests add a small POST route to the app they build.
A POST to an unknown path would give 404 or 405 and never reach the route code. These
tests need a route that reads the body.
"""

import pytest
from fastapi import FastAPI, Request
from fastapi.testclient import TestClient
from starlette.types import Message, Scope

from chb.app import create_app
from chb.request_limits import RequestLimits, client_ip, limits_from_env


def app_with_echo(limits: RequestLimits, clock: list[float] | None = None) -> FastAPI:
    now = clock if clock is not None else [0.0]
    app = create_app(limits=limits, clock=lambda: now[0])

    @app.post("/api/echo")
    async def echo(request: Request) -> dict[str, int]:  # pyright: ignore[reportUnusedFunction]
        return {"size": len(await request.body())}

    @app.put("/api/echo")
    async def put_echo(request: Request) -> dict[str, int]:  # pyright: ignore[reportUnusedFunction]
        return {"size": len(await request.body())}

    return app


SMALL = RequestLimits(max_body_bytes=100, writes_per_minute=3, trusted_proxy_hops=1)


# Acceptance: the three behaviours of the ticket.


def test_a_body_over_the_limit_gets_413() -> None:
    client = TestClient(app_with_echo(SMALL))

    response = client.post("/api/echo", content=b"x" * 101)

    assert response.status_code == 413
    assert "content-security-policy" in response.headers


def test_requests_over_the_rate_get_429_with_retry_after() -> None:
    client = TestClient(app_with_echo(SMALL))

    codes = [client.post("/api/echo", content=b"x").status_code for _ in range(4)]

    assert codes == [200, 200, 200, 429]
    blocked = client.post("/api/echo", content=b"x")
    assert blocked.status_code == 429
    assert int(blocked.headers["retry-after"]) == 60
    assert "content-security-policy" in blocked.headers


def test_normal_traffic_under_the_limits_passes() -> None:
    client = TestClient(app_with_echo(SMALL))

    for _ in range(3):
        response = client.post("/api/echo", content=b"x" * 100)
        assert response.status_code == 200
        assert response.json() == {"size": 100}
    assert client.get("/api/health").status_code == 200


# Size limit details.


def test_a_declared_oversize_body_is_refused_before_the_route_reads_it() -> None:
    reads: list[int] = []
    app = create_app(limits=SMALL)

    @app.post("/api/probe")
    async def probe(request: Request) -> dict[str, int]:  # pyright: ignore[reportUnusedFunction]
        reads.append(1)
        return {"size": len(await request.body())}

    response = TestClient(app).post("/api/probe", content=b"x" * 5000)

    assert response.status_code == 413
    assert reads == []


@pytest.mark.anyio
async def test_a_chunked_body_over_the_limit_stops_after_the_limit() -> None:
    """Drive the ASGI app directly: the test client reads a whole body before it sends it."""
    app = app_with_echo(SMALL)
    chunks_pulled = 0
    sent: list[Message] = []

    async def receive() -> Message:
        nonlocal chunks_pulled
        chunks_pulled += 1
        return {"type": "http.request", "body": b"x" * 60, "more_body": True}

    async def send(message: Message) -> None:
        sent.append(message)

    scope: Scope = {
        "type": "http",
        "asgi": {"version": "3.0"},
        "http_version": "1.1",
        "method": "POST",
        "path": "/api/echo",
        "raw_path": b"/api/echo",
        "query_string": b"",
        "headers": [(b"transfer-encoding", b"chunked")],
        "client": ("198.51.100.9", 1),
        "server": ("testserver", 80),
        "scheme": "http",
    }

    await app(scope, receive, send)

    assert sent[0]["status"] == 413
    assert chunks_pulled == 2  # 60 bytes pass, the second chunk crosses 100 and stops the read


def test_a_chunked_body_under_the_limit_passes() -> None:
    client = TestClient(app_with_echo(SMALL))

    response = client.post("/api/echo", content=iter([b"x" * 40, b"y" * 40]))

    assert response.status_code == 200
    assert response.json() == {"size": 80}


def test_a_body_of_exactly_the_limit_passes() -> None:
    client = TestClient(app_with_echo(SMALL))

    assert client.post("/api/echo", content=b"x" * 100).status_code == 200


def test_a_bad_content_length_is_not_trusted() -> None:
    client = TestClient(app_with_echo(SMALL))

    response = client.post("/api/echo", content=b"x" * 101, headers={"content-length": "abc"})

    assert response.status_code in (400, 413)


# Rate limit details.


def test_get_requests_are_not_rate_limited() -> None:
    client = TestClient(app_with_echo(SMALL))

    assert all(client.get("/api/health").status_code == 200 for _ in range(20))


def test_all_write_methods_count_together() -> None:
    client = TestClient(app_with_echo(SMALL))

    client.post("/api/echo", content=b"x")
    client.put("/api/echo", content=b"x")
    client.post("/api/echo", content=b"x")

    assert client.put("/api/echo", content=b"x").status_code == 429


def test_the_window_resets_after_a_minute_and_retry_after_counts_down() -> None:
    clock = [100.0]
    client = TestClient(app_with_echo(SMALL, clock))
    for _ in range(3):
        client.post("/api/echo", content=b"x")

    clock[0] = 130.0
    blocked = client.post("/api/echo", content=b"x")
    assert blocked.status_code == 429
    assert int(blocked.headers["retry-after"]) == 30

    clock[0] = 160.0
    assert client.post("/api/echo", content=b"x").status_code == 200


def test_each_client_ip_has_its_own_budget() -> None:
    client = TestClient(app_with_echo(SMALL))
    first = {"x-forwarded-for": "198.51.100.1"}
    second = {"x-forwarded-for": "198.51.100.2"}
    for _ in range(3):
        client.post("/api/echo", content=b"x", headers=first)

    assert client.post("/api/echo", content=b"x", headers=first).status_code == 429
    assert client.post("/api/echo", content=b"x", headers=second).status_code == 200


def test_a_client_cannot_dodge_the_limit_by_forging_the_left_of_x_forwarded_for() -> None:
    client = TestClient(app_with_echo(SMALL))
    for i in range(3):
        client.post(
            "/api/echo", content=b"x", headers={"x-forwarded-for": f"10.0.0.{i}, 198.51.100.1"}
        )

    response = client.post(
        "/api/echo", content=b"x", headers={"x-forwarded-for": "10.9.9.9, 198.51.100.1"}
    )

    assert response.status_code == 429


def test_the_table_of_clients_stays_bounded() -> None:
    limits = RequestLimits(max_body_bytes=100, writes_per_minute=1, max_tracked_clients=5)
    app = app_with_echo(limits)
    client = TestClient(app)

    for i in range(50):
        client.post("/api/echo", content=b"x", headers={"x-forwarded-for": f"198.51.100.{i}"})

    assert app.state.rate_limiter.tracked_clients() <= 5


# Client IP.


def test_client_ip_takes_the_entry_the_trusted_proxy_appended() -> None:
    scope = {"client": ("10.1.1.1", 1234), "headers": [(b"x-forwarded-for", b"1.1.1.1, 2.2.2.2")]}

    assert client_ip(scope, hops=1) == "2.2.2.2"
    assert client_ip(scope, hops=2) == "1.1.1.1"


def test_client_ip_falls_back_to_the_socket_peer() -> None:
    no_header: dict[str, object] = {"client": ("10.1.1.1", 1234), "headers": []}
    short_header: dict[str, object] = {
        "client": ("10.1.1.1", 1234),
        "headers": [(b"x-forwarded-for", b"2.2.2.2")],
    }
    no_client: dict[str, object] = {"client": None, "headers": []}

    assert client_ip(no_header, hops=1) == "10.1.1.1"
    assert client_ip(short_header, hops=2) == "10.1.1.1"
    assert client_ip(no_client, hops=1) == "unknown"


def test_hops_zero_ignores_the_header() -> None:
    scope = {"client": ("10.1.1.1", 1), "headers": [(b"x-forwarded-for", b"2.2.2.2")]}

    assert client_ip(scope, hops=0) == "10.1.1.1"


# Settings.


def test_defaults() -> None:
    limits = limits_from_env({})

    assert limits == RequestLimits(
        max_body_bytes=65_536, writes_per_minute=60, trusted_proxy_hops=1
    )


def test_settings_come_from_the_environment() -> None:
    limits = limits_from_env(
        {
            "MAX_REQUEST_BODY_BYTES": "1024",
            "RATE_LIMIT_WRITES_PER_MINUTE": "5",
            "TRUSTED_PROXY_HOPS": "0",
        }
    )

    assert limits == RequestLimits(max_body_bytes=1024, writes_per_minute=5, trusted_proxy_hops=0)


@pytest.mark.parametrize(
    "env",
    [
        {"MAX_REQUEST_BODY_BYTES": "0"},
        {"MAX_REQUEST_BODY_BYTES": "lots"},
        {"RATE_LIMIT_WRITES_PER_MINUTE": "-1"},
        {"TRUSTED_PROXY_HOPS": "-1"},
    ],
)
def test_invalid_settings_stop_the_start(env: dict[str, str]) -> None:
    with pytest.raises(ValueError, match=next(iter(env))):
        limits_from_env(env)
