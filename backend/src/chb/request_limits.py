"""Request size limit and per-client rate limit (CHB-23).

The repositories are in memory, so one oversized request or a flood can crash the container.
Two pure ASGI middlewares, so they run before any route code and need no new dependency:

- `BodySizeLimitMiddleware` answers 413 when a body is over the limit. It checks the
  Content-Length header first, and counts the bytes of a chunked body as they arrive. It stops
  reading at the limit, so it never holds more than the limit plus one chunk.
- `RateLimitMiddleware` answers 429 with `Retry-After` when a client IP sends more write
  requests (POST, PUT, PATCH, DELETE) per minute than allowed. Counters live in this
  process: Cloud Run runs one instance (ADR-d5e6), so the count is exact. With more
  instances each one counts on its own.
"""

import json
import math
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import Any

from starlette.types import ASGIApp, Message, Receive, Scope, Send

WRITE_METHODS = frozenset({"POST", "PUT", "PATCH", "DELETE"})
WINDOW_SECONDS = 60.0


@dataclass(frozen=True)
class RequestLimits:
    max_body_bytes: int = 65_536
    writes_per_minute: int = 60
    # How many proxies in front of the app append to X-Forwarded-For. Cloud Run: 1.
    trusted_proxy_hops: int = 1
    # Upper bound on the rate limiter's table, so a flood from many IPs cannot grow it forever.
    max_tracked_clients: int = 10_000


def _int_setting(env: Mapping[str, str], name: str, default: int, minimum: int) -> int:
    raw = env.get(name)
    if raw is None or raw == "":
        return default
    try:
        value = int(raw)
    except ValueError:
        raise ValueError(f"{name} must be an integer, got {raw!r}") from None
    if value < minimum:
        raise ValueError(f"{name} must be at least {minimum}, got {value}")
    return value


def limits_from_env(env: Mapping[str, str]) -> RequestLimits:
    defaults = RequestLimits()
    return RequestLimits(
        max_body_bytes=_int_setting(env, "MAX_REQUEST_BODY_BYTES", defaults.max_body_bytes, 1),
        writes_per_minute=_int_setting(
            env, "RATE_LIMIT_WRITES_PER_MINUTE", defaults.writes_per_minute, 1
        ),
        trusted_proxy_hops=_int_setting(env, "TRUSTED_PROXY_HOPS", defaults.trusted_proxy_hops, 0),
    )


def client_ip(scope: Mapping[str, Any], hops: int) -> str:
    """The client IP, read from the right of X-Forwarded-For.

    Each trusted proxy appends the address it received the request from. The entries to the
    left of those are written by the client and can be forged, so they are never used.
    Cloud Run appends the caller's address: with hops=1 the last entry is the client.
    With hops=0, or when the header has fewer entries than hops, the socket peer is used.
    """
    if hops > 0:
        # A client can send several X-Forwarded-For lines. Proxies append to the last one,
        # so read all lines as one list, in order.
        entries = [
            e.strip()
            for name, value in scope.get("headers", [])
            if name == b"x-forwarded-for"
            for e in value.decode("latin-1").split(",")
            if e.strip()
        ]
        if len(entries) >= hops:
            return entries[-hops]
    peer = scope.get("client")
    return peer[0] if peer else "unknown"


async def _reject(
    send: Send, status: int, detail: str, extra_headers: list[tuple[bytes, bytes]] | None = None
) -> None:
    body = json.dumps({"detail": detail}).encode()
    headers = [
        (b"content-type", b"application/json"),
        (b"content-length", str(len(body)).encode()),
        # Close the connection: an unread body is not worth draining.
        (b"connection", b"close"),
        *(extra_headers or []),
    ]
    await send({"type": "http.response.start", "status": status, "headers": headers})
    await send({"type": "http.response.body", "body": body})


class _TooLarge(Exception):
    pass


class BodySizeLimitMiddleware:
    def __init__(self, app: ASGIApp, max_body_bytes: int) -> None:
        self.app = app
        self.max_body_bytes = max_body_bytes

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        declared = self._declared_length(scope)
        if declared == "invalid":
            await _reject(send, 400, "Invalid Content-Length header.")
            return
        if isinstance(declared, int) and declared > self.max_body_bytes:
            await _reject(send, 413, self._message())
            return

        received = 0
        started = False

        async def counting_receive() -> Message:
            nonlocal received
            message = await receive()
            if message["type"] == "http.request":
                received += len(message.get("body", b""))
                if received > self.max_body_bytes:
                    raise _TooLarge
            return message

        async def tracking_send(message: Message) -> None:
            nonlocal started
            if message["type"] == "http.response.start":
                started = True
            await send(message)

        try:
            await self.app(scope, counting_receive, tracking_send)
        except _TooLarge:
            if started:
                raise
            await _reject(send, 413, self._message())

    def _message(self) -> str:
        return f"Request body is larger than {self.max_body_bytes} bytes."

    @staticmethod
    def _declared_length(scope: Scope) -> int | str | None:
        for name, value in scope.get("headers", []):
            if name == b"content-length":
                text = value.decode("latin-1").strip()
                return int(text) if text.isascii() and text.isdigit() else "invalid"
        return None


class RateLimiter:
    """Fixed window per client: at most `limit` hits in each `WINDOW_SECONDS` span."""

    def __init__(self, limit: int, clock: Callable[[], float], max_clients: int = 10_000) -> None:
        self.limit = limit
        self.clock = clock
        self.max_clients = max_clients
        self._windows: dict[str, tuple[float, int]] = {}

    def hit(self, client: str) -> int:
        """Count one request. Return 0 when allowed, else the seconds to wait."""
        now = self.clock()
        start, count = self._windows.get(client, (now, 0))
        if now - start >= WINDOW_SECONDS:
            start, count = now, 0
        if count >= self.limit:
            return max(1, math.ceil(start + WINDOW_SECONDS - now))
        if client not in self._windows and len(self._windows) >= self.max_clients:
            self._make_room(now)
        self._windows[client] = (start, count + 1)
        return 0

    def tracked_clients(self) -> int:
        return len(self._windows)

    def _make_room(self, now: float) -> None:
        self._windows = {c: w for c, w in self._windows.items() if now - w[0] < WINDOW_SECONDS}
        while len(self._windows) >= self.max_clients:
            # Still full of live windows: forget the oldest one.
            del self._windows[next(iter(self._windows))]


class RateLimitMiddleware:
    def __init__(self, app: ASGIApp, limiter: RateLimiter, hops: int) -> None:
        self.app = app
        self.limiter = limiter
        self.hops = hops

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] == "http" and scope["method"] in WRITE_METHODS:
            wait = self.limiter.hit(client_ip(scope, self.hops))
            if wait:
                await _reject(
                    send,
                    429,
                    "Too many requests. Try again later.",
                    [(b"retry-after", str(wait).encode())],
                )
                return
        await self.app(scope, receive, send)
