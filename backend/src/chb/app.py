"""FastAPI application: the JSON API under /api and, in production, the built frontend."""

import logging
import os
from collections.abc import AsyncGenerator, Mapping
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Annotated

from fastapi import Depends, FastAPI
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel
from starlette.types import ASGIApp

from chb.auth import (
    CachedCerts,
    FirebaseTokenVerifier,
    RejectAll,
    TokenVerifier,
    User,
    WebConfig,
    authenticate,
    current_user,
    fetch_google_certs,
)
from chb.logs import configure_logging
from chb.telemetry import Telemetry, telemetry_from_env

logger = logging.getLogger(__name__)


class Health(BaseModel):
    status: str
    version: str


class Me(BaseModel):
    uid: str
    email: str | None


def create_app(
    static_dir: Path | None = None,
    telemetry: Telemetry | None = None,
    verifier: TokenVerifier | None = None,
    web_config: WebConfig | None = None,
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
        docs_url="/api/docs",
        redoc_url="/api/redoc",
        openapi_url="/api/openapi.json",
        # Closed by default: every route checks the ID token unless its path is public (ADR-50e4).
        dependencies=[Depends(authenticate)],
    )
    app.state.token_verifier = verifier if verifier is not None else RejectAll()

    @app.get("/api/health")
    def health() -> Health:  # pyright: ignore[reportUnusedFunction]
        return Health(status="ok", version=os.environ.get("APP_VERSION", "dev"))

    @app.get("/api/me")
    def me(user: Annotated[User, Depends(current_user)]) -> Me:  # pyright: ignore[reportUnusedFunction]
        return Me(uid=user.uid, email=user.email)

    # Outside /api: part of the public frontend. 404 until Terraform sets the values (CHB-20).
    @app.get("/auth-config.json", include_in_schema=False)
    def auth_config() -> JSONResponse:  # pyright: ignore[reportUnusedFunction]
        if web_config is None:
            return JSONResponse({"detail": "Sign-in is not configured"}, status_code=404)
        return JSONResponse(
            {"apiKey": web_config.api_key, "authDomain": web_config.auth_domain},
            headers={"Cache-Control": "no-cache"},
        )

    if static_dir is not None and static_dir.is_dir():
        app.mount("/", StaticFiles(directory=static_dir, html=True), name="frontend")

    if telemetry is not None:
        telemetry.instrument(app)

    return app


def verifier_from_env(env: Mapping[str, str]) -> TokenVerifier:
    project_id = env.get("FIREBASE_PROJECT_ID")
    if not project_id:
        return RejectAll()
    return FirebaseTokenVerifier(project_id, CachedCerts(fetch_google_certs))


def web_config_from_env(env: Mapping[str, str]) -> WebConfig | None:
    api_key, auth_domain = env.get("FIREBASE_API_KEY"), env.get("FIREBASE_AUTH_DOMAIN")
    return WebConfig(api_key, auth_domain) if api_key and auth_domain else None


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
        Path(static_dir) if static_dir else None,
        telemetry,
        verifier=verifier_from_env(env),
        web_config=web_config_from_env(env),
    )
    return telemetry.wrap(fastapi_app) if telemetry is not None else fastapi_app


app = app_from_env(os.environ)
