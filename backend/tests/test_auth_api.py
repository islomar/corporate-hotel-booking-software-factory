"""Acceptance: every route needs a valid Firebase ID token unless listed public (ADR-50e4)."""

import time
from collections.abc import Mapping
from pathlib import Path

import pytest
from conftest import TokenIssuer
from fastapi.routing import APIRoute
from fastapi.testclient import TestClient

from chb.app import app_from_env, create_app
from chb.auth import (
    FRONTEND_PATHS,
    PUBLIC_PATHS,
    CachedCerts,
    FirebaseTokenVerifier,
    User,
    WebConfig,
)


@pytest.fixture
def client(issuer: TokenIssuer) -> TestClient:
    verifier = FirebaseTokenVerifier(issuer.project_id, CachedCerts(issuer.fetch_certs))
    return TestClient(create_app(verifier=verifier))


def bearer(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


def assert_unauthorized(response_status: int, headers: dict[str, str]) -> None:
    assert response_status == 401
    assert headers.get("www-authenticate") == "Bearer"


def test_valid_token_reaches_a_closed_route(client: TestClient, issuer: TokenIssuer) -> None:
    response = client.get("/api/me", headers=bearer(issuer.token()))

    assert response.status_code == 200
    assert response.json() == {
        "uid": "user-123",
        "email": "ada@example.com",
        "email_verified": True,
    }


def test_missing_token_gets_401(client: TestClient) -> None:
    response = client.get("/api/me")

    assert_unauthorized(response.status_code, dict(response.headers))


def test_expired_token_gets_401(client: TestClient, issuer: TokenIssuer) -> None:
    now = int(time.time())
    token = issuer.token(iat=now - 7200, auth_time=now - 7200, exp=now - 3600)

    response = client.get("/api/me", headers=bearer(token))

    assert_unauthorized(response.status_code, dict(response.headers))


def test_token_for_another_project_gets_401(client: TestClient, issuer: TokenIssuer) -> None:
    token = issuer.token(aud="another-project")

    response = client.get("/api/me", headers=bearer(token))

    assert_unauthorized(response.status_code, dict(response.headers))


def test_token_from_another_issuer_gets_401(client: TestClient, issuer: TokenIssuer) -> None:
    token = issuer.token(iss="https://securetoken.google.com/another-project")

    response = client.get("/api/me", headers=bearer(token))

    assert_unauthorized(response.status_code, dict(response.headers))


def test_token_signed_by_an_unknown_key_gets_401(client: TestClient, stranger: TokenIssuer) -> None:
    response = client.get("/api/me", headers=bearer(stranger.token()))

    assert_unauthorized(response.status_code, dict(response.headers))


def test_malformed_header_gets_401(client: TestClient) -> None:
    for header in ("Bearer", "Bearer ", "Basic abc", "garbage", "Bearer not-a-jwt"):
        response = client.get("/api/me", headers={"Authorization": header})

        assert_unauthorized(response.status_code, dict(response.headers))


@pytest.mark.parametrize("path", sorted(PUBLIC_PATHS))
def test_public_paths_need_no_token(client: TestClient, path: str) -> None:
    assert client.get(path).status_code == 200


def test_public_paths_are_exactly_the_adr_list() -> None:
    assert {"/api/health", "/api/docs", "/api/redoc", "/api/openapi.json"} == PUBLIC_PATHS


def test_the_frontend_needs_only_its_web_config() -> None:
    assert {"/auth-config.json"} == FRONTEND_PATHS


def test_every_route_outside_the_public_lists_is_closed(tmp_path: Path) -> None:
    """A new route without the auth dependency fails here, before it reaches production."""
    (tmp_path / "index.html").write_text("<h1>frontend</h1>")
    app = create_app(static_dir=tmp_path, web_config=WebConfig("key", "example.firebaseapp.com"))
    client = TestClient(app)
    closed = [
        (route.path, method)
        for route in app.routes
        if isinstance(route, APIRoute) and route.path not in PUBLIC_PATHS | FRONTEND_PATHS
        for method in route.methods or ()
    ]

    assert closed, "expected at least one closed route"
    for path, method in closed:
        response = client.request(method, path)
        assert response.status_code == 401, f"{method} {path} answered {response.status_code}"


def test_no_plain_route_or_mount_bypasses_the_check_under_api(tmp_path: Path) -> None:
    """App-wide dependencies run on API routes only: a plain route or a mount would be open."""
    (tmp_path / "index.html").write_text("<h1>frontend</h1>")
    app = create_app(static_dir=tmp_path, web_config=WebConfig("key", "example.firebaseapp.com"))
    unchecked = [
        getattr(route, "path", "")
        for route in app.routes
        if not isinstance(route, APIRoute)
        and getattr(route, "path", "").startswith("/api")
        and getattr(route, "path", "") not in PUBLIC_PATHS
    ]

    assert unchecked == []


def test_without_a_project_every_closed_route_rejects_tokens(issuer: TokenIssuer) -> None:
    client = TestClient(create_app())

    response = client.get("/api/me", headers=bearer(issuer.token()))

    assert_unauthorized(response.status_code, dict(response.headers))


def test_frontend_reads_the_public_web_config(tmp_path: Path) -> None:
    (tmp_path / "index.html").write_text("<h1>frontend</h1>")
    app = create_app(static_dir=tmp_path, web_config=WebConfig("browser-key", "p.firebaseapp.com"))

    response = TestClient(app).get("/auth-config.json")

    assert response.status_code == 200
    assert response.json() == {"apiKey": "browser-key", "authDomain": "p.firebaseapp.com"}
    assert response.headers["cache-control"] == "no-cache"


def test_web_config_is_absent_until_terraform_sets_it(tmp_path: Path) -> None:
    (tmp_path / "index.html").write_text("<h1>frontend</h1>")

    response = TestClient(create_app(static_dir=tmp_path)).get("/auth-config.json")

    assert response.status_code == 404


def test_production_app_reads_auth_settings_from_the_environment(issuer: TokenIssuer) -> None:
    env = {
        "FIREBASE_PROJECT_ID": issuer.project_id,
        "FIREBASE_API_KEY": "browser-key",
        "FIREBASE_AUTH_DOMAIN": "p.firebaseapp.com",
    }
    client = TestClient(app_from_env(env))

    assert client.get("/auth-config.json").json()["apiKey"] == "browser-key"
    assert client.get("/api/me").status_code == 401


def test_unreachable_google_certs_answer_503_not_401(issuer: TokenIssuer) -> None:
    def failing() -> tuple[Mapping[str, str], float]:
        raise OSError("network down")

    app = create_app(verifier=FirebaseTokenVerifier(issuer.project_id, CachedCerts(failing)))

    response = TestClient(app).get("/api/me", headers=bearer(issuer.token()))

    assert response.status_code == 503


def test_a_request_checks_its_token_once(issuer: TokenIssuer) -> None:
    calls: list[str] = []
    inner = FirebaseTokenVerifier(issuer.project_id, CachedCerts(issuer.fetch_certs))

    class Counting:
        def verify(self, token: str) -> User:
            calls.append(token)
            return inner.verify(token)

    TestClient(create_app(verifier=Counting())).get("/api/me", headers=bearer(issuer.token()))

    assert len(calls) == 1
