"""FastAPI application: the JSON API under /api and, in production, the built frontend."""

import logging
import os
from collections.abc import AsyncGenerator, Mapping
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel
from starlette.types import ASGIApp

from chb.logs import configure_logging
from chb.telemetry import Telemetry, telemetry_from_env

logger = logging.getLogger(__name__)


class Health(BaseModel):
    status: str
    version: str


def create_app(static_dir: Path | None = None, telemetry: Telemetry | None = None) -> FastAPI:
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
        docs_url="/api/docs",
        redoc_url="/api/redoc",
        openapi_url="/api/openapi.json",
    )

    @app.get("/api/health")
    def health() -> Health:  # pyright: ignore[reportUnusedFunction]
        return Health(status="ok", version=os.environ.get("APP_VERSION", "dev"))

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
    static_dir = env.get("STATIC_DIR")
    fastapi_app = create_app(Path(static_dir) if static_dir else None, telemetry)
    return telemetry.wrap(fastapi_app) if telemetry is not None else fastapi_app


app = app_from_env(os.environ)
