"""FastAPI application: the JSON API under /api and, in production, the built frontend."""

import os
from pathlib import Path

from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel


class Health(BaseModel):
    status: str
    version: str


def create_app(static_dir: Path | None = None) -> FastAPI:
    app = FastAPI(
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

    return app


def _static_dir_from_env() -> Path | None:
    value = os.environ.get("STATIC_DIR")
    return Path(value) if value else None


app = create_app(_static_dir_from_env())
