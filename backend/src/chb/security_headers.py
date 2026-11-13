"""Security response headers on every response (CHB-22).

A pure ASGI middleware, so it also covers static files, 404s and streamed bodies.
It sits next to other cross-cutting middleware; add new ones in `create_app` in `chb.app`.
"""

from collections.abc import Mapping

from starlette.types import ASGIApp, Message, Receive, Scope, Send

ONE_YEAR_SECONDS = 31_536_000

# default-src 'self' covers script, style, image, font, connect, frame and worker sources.
CSP_DIRECTIVES: Mapping[str, str] = {
    "default-src": "'self'",
    "frame-ancestors": "'none'",
    "object-src": "'none'",
    "base-uri": "'self'",
    "form-action": "'self'",
}

# Swagger UI draws its icons as data: URIs in its CSS. Nothing else is loosened.
DOCS_CSP_EXTRA: Mapping[str, str] = {"img-src": "'self' data:"}
DOCS_PATH = "/api/docs"

SECURITY_HEADERS: Mapping[str, str] = {
    "strict-transport-security": f"max-age={ONE_YEAR_SECONDS}",
    "x-content-type-options": "nosniff",
    "referrer-policy": "strict-origin-when-cross-origin",
}


def _csp(extra: Mapping[str, str]) -> str:
    return "; ".join(f"{name} {value}" for name, value in {**CSP_DIRECTIVES, **extra}.items())


DEFAULT_CSP = _csp({})
DOCS_CSP = _csp(DOCS_CSP_EXTRA)


def csp_for(path: str) -> str:
    return DOCS_CSP if path == DOCS_PATH else DEFAULT_CSP


class SecurityHeadersMiddleware:
    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        csp = csp_for(scope["path"])

        async def send_with_headers(message: Message) -> None:
            if message["type"] == "http.response.start":
                headers: list[tuple[bytes, bytes]] = list(message.get("headers", []))
                present = {name.lower() for name, _ in headers}
                wanted: dict[str, str] = {
                    "content-security-policy": csp,
                    **SECURITY_HEADERS,
                }
                headers.extend(
                    (name.encode(), value.encode())
                    for name, value in wanted.items()
                    if name.encode() not in present
                )
                message = {**message, "headers": headers}
            await send(message)

        await self.app(scope, receive, send_with_headers)
