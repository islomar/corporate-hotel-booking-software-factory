"""FastAPI application: the JSON API under /api and, in production, the built frontend."""

import os
from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from chb.logs import configure_logging
from chb.telemetry import Telemetry, project_id_from_env, telemetry_from_env


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


def _static_dir_from_env() -> Path | None:
    value = os.environ.get("STATIC_DIR")
    return Path(value) if value else None


def _app_from_env() -> FastAPI:
    telemetry = telemetry_from_env(os.environ, version=os.environ.get("APP_VERSION", "dev"))
    log_handler = telemetry.log_handler() if telemetry is not None else None
    configure_logging(project_id_from_env(), [log_handler] if log_handler else [])
    return create_app(_static_dir_from_env(), telemetry)


app = _app_from_env()
