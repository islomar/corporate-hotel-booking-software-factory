"""FastAPI application: the JSON API under /api and, in production, the built frontend."""

import logging
import os
import time
from collections.abc import AsyncGenerator, Callable, Mapping
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI
from fastapi.responses import HTMLResponse, Response
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel
from starlette.types import ASGIApp

from chb.logs import configure_logging
from chb.request_limits import (
    BodySizeLimitMiddleware,
    RateLimiter,
    RateLimitMiddleware,
    RequestLimits,
    limits_from_env,
)
from chb.security_headers import DOCS_PATH, SecurityHeadersMiddleware
from chb.telemetry import Telemetry, telemetry_from_env

logger = logging.getLogger(__name__)


# The frontend build copies swagger-ui-dist into <static dir>/swagger-ui (frontend/vite.config.ts).
SWAGGER_UI_FOLDER = "swagger-ui"
DOCS_ASSETS_PATH = "/api/docs-assets"
SWAGGER_UI_HTML = f"""<!doctype html>
<html lang="en">
  <head>
    <meta charset="UTF-8" />
    <meta name="viewport" content="width=device-width, initial-scale=1.0" />
    <title>Corporate Hotel Booking: API documentation</title>
    <link rel="stylesheet" href="{DOCS_ASSETS_PATH}/swagger-ui.css" />
    <link rel="icon" type="image/png" href="{DOCS_ASSETS_PATH}/favicon-32x32.png" />
  </head>
  <body>
    <div id="swagger-ui"></div>
    <script src="{DOCS_ASSETS_PATH}/swagger-ui-bundle.js"></script>
    <script src="{DOCS_ASSETS_PATH}/init.js"></script>
  </body>
</html>
"""
# A separate file, not an inline script: the CSP allows no inline script.
SWAGGER_UI_INIT_JS = """window.ui = SwaggerUIBundle({
  url: "/api/openapi.json",
  dom_id: "#swagger-ui",
  deepLinking: true,
  presets: [SwaggerUIBundle.presets.apis],
});
"""


class Health(BaseModel):
    status: str
    version: str


def create_app(
    static_dir: Path | None = None,
    telemetry: Telemetry | None = None,
    limits: RequestLimits | None = None,
    clock: Callable[[], float] = time.monotonic,
) -> FastAPI:
    @asynccontextmanager
    async def lifespan(_: FastAPI) -> AsyncGenerator[None]:
        yield
        # Cloud Run gives CPU during shutdown: export what is left.
        if telemetry is not None:
            telemetry.shutdown()

    app = FastAPI(
        lifespan=lifespan,
        title="Corporate Hotel Booking",
        summary="Book hotel rooms for company employees under company booking policies.",
        # Swagger UI is served below from the app's own files. FastAPI's default page loads
        # it from a CDN and runs an inline script, which the CSP forbids. ReDoc has no
        # self-hosted copy, so it is off.
        docs_url=None,
        redoc_url=None,
        openapi_url="/api/openapi.json",
    )

    limits = limits or RequestLimits()
    limiter = RateLimiter(limits.writes_per_minute, clock, limits.max_tracked_clients)
    app.state.rate_limiter = limiter
    # The last middleware added is the outermost. Security headers go on the 413 and 429 too.
    app.add_middleware(BodySizeLimitMiddleware, max_body_bytes=limits.max_body_bytes)
    app.add_middleware(RateLimitMiddleware, limiter=limiter, hops=limits.trusted_proxy_hops)
    app.add_middleware(SecurityHeadersMiddleware)

    @app.get(DOCS_PATH, include_in_schema=False)
    def swagger_ui() -> HTMLResponse:  # pyright: ignore[reportUnusedFunction]
        return HTMLResponse(SWAGGER_UI_HTML)

    @app.get(f"{DOCS_ASSETS_PATH}/init.js", include_in_schema=False)
    def swagger_ui_init() -> Response:  # pyright: ignore[reportUnusedFunction]
        return Response(SWAGGER_UI_INIT_JS, media_type="text/javascript")

    @app.get("/api/health")
    def health() -> Health:  # pyright: ignore[reportUnusedFunction]
        return Health(status="ok", version=os.environ.get("APP_VERSION", "dev"))

    if static_dir is not None and (static_dir / SWAGGER_UI_FOLDER).is_dir():
        app.mount(
            DOCS_ASSETS_PATH,
            StaticFiles(directory=static_dir / SWAGGER_UI_FOLDER),
            name="swagger-ui",
        )

    if static_dir is not None and static_dir.is_dir():
        app.mount("/", StaticFiles(directory=static_dir, html=True), name="frontend")

    if telemetry is not None:
        telemetry.instrument(app)

    return app


def app_from_env(env: Mapping[str, str]) -> ASGIApp:
    """The production app. Telemetry fails open: without it the app still starts and serves."""
    error: Exception | None = None
    try:
        telemetry = telemetry_from_env(env, version=env.get("APP_VERSION", "dev"))
    except Exception as exc:
        telemetry, error = None, exc
    log_handler = telemetry.log_handler() if telemetry is not None else None
    configure_logging(env.get("GOOGLE_CLOUD_PROJECT"), [log_handler] if log_handler else [])
    if error is not None:
        logger.warning("Telemetry is off: setup failed: %r", error)
    if telemetry is not None and telemetry.metrics_off_reason is not None:
        logger.warning(telemetry.metrics_off_reason)
    static_dir = env.get("STATIC_DIR")
    fastapi_app = create_app(
        Path(static_dir) if static_dir else None, telemetry, limits_from_env(env)
    )
    return telemetry.wrap(fastapi_app) if telemetry is not None else fastapi_app


app = app_from_env(os.environ)
