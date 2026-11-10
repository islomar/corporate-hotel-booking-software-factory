"""Sign-in with Identity Platform (ADR-50e4): every /api route is closed unless listed public.

The frontend signs in with the Firebase JS SDK and sends the ID token as
`Authorization: Bearer <token>`. The backend checks the signature against Google's published
certificates, then the claims Firebase documents: `aud` and `iss` name our project, `exp` and
`iat` hold, `sub` is a non-empty user ID and `auth_time` is in the past.
See https://firebase.google.com/docs/auth/admin/verify-id-tokens#verify_id_tokens_using_a_third-party_jwt_library.
"""

import re
import threading
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import Annotated, Any, Protocol

import requests
from fastapi import Depends, HTTPException, Request
from google.auth import exceptions, jwt

# Reachable without a token: the API list of ADR-50e4. Every other route needs one.
PUBLIC_PATHS = frozenset({"/api/health", "/api/docs", "/api/redoc", "/api/openapi.json"})
# The frontend is public too (ADR-50e4): static files, mounted outside the routes, and the
# web config it needs before anyone can sign in.
FRONTEND_PATHS = frozenset({"/auth-config.json"})

CERTS_URL = (
    "https://www.googleapis.com/robot/v1/metadata/x509/securetoken@system.gserviceaccount.com"
)

type CertsFetch = Callable[[], tuple[Mapping[str, str], float]]


@dataclass(frozen=True)
class User:
    uid: str
    email: str | None


@dataclass(frozen=True)
class WebConfig:
    """The Firebase JS SDK config. Public by design: it names the project, it grants nothing."""

    api_key: str
    auth_domain: str


class InvalidTokenError(Exception):
    """The token is missing, malformed, expired, or not ours."""


class CertsUnavailableError(Exception):
    """Google's signing certificates could not be fetched: the token cannot be checked now."""


class TokenVerifier(Protocol):
    def verify(self, token: str) -> User: ...


class RejectAll:
    """Without a project to check against, no token is valid: closed, not open."""

    def verify(self, token: str) -> User:
        raise InvalidTokenError("sign-in is not configured")


def max_age_seconds(cache_control: str) -> float:
    match = re.search(r"max-age=(\d+)", cache_control)
    return float(match.group(1)) if match else 0.0


def fetch_google_certs() -> tuple[Mapping[str, str], float]:
    response = requests.get(CERTS_URL, timeout=5)
    response.raise_for_status()
    certs: dict[str, str] = response.json()
    return certs, max_age_seconds(response.headers.get("Cache-Control", ""))


class CachedCerts:
    """Keeps the certificates for as long as Google's Cache-Control allows."""

    def __init__(self, fetch: CertsFetch, clock: Callable[[], float] = time.monotonic) -> None:
        self._fetch = fetch
        self._clock = clock
        self._lock = threading.Lock()
        self._certs: Mapping[str, str] = {}
        self._expires_at = float("-inf")

    def __call__(self) -> Mapping[str, str]:
        with self._lock:
            if self._clock() >= self._expires_at:
                certs, max_age = self._fetch()
                self._certs, self._expires_at = certs, self._clock() + max_age
            return self._certs


class FirebaseTokenVerifier:
    def __init__(self, project_id: str, certs: Callable[[], Mapping[str, str]]) -> None:
        self._project_id = project_id
        self._certs = certs

    def verify(self, token: str) -> User:
        try:
            certs = self._certs()
        except Exception as exc:
            raise CertsUnavailableError(repr(exc)) from exc
        try:
            claims: Mapping[str, Any] = jwt.decode(  # pyright: ignore[reportUnknownMemberType]
                token, certs=certs, audience=self._project_id
            )
        except (ValueError, exceptions.GoogleAuthError) as exc:
            raise InvalidTokenError(str(exc)) from exc
        if claims.get("iss") != f"https://securetoken.google.com/{self._project_id}":
            raise InvalidTokenError("wrong issuer")
        uid = claims.get("sub")
        if not isinstance(uid, str) or not uid:
            raise InvalidTokenError("no subject")
        auth_time = claims.get("auth_time")
        if not isinstance(auth_time, int | float) or auth_time > time.time():
            raise InvalidTokenError("sign-in time missing or in the future")
        email = claims.get("email")
        return User(uid=uid, email=email if isinstance(email, str) else None)


def _unauthorized(detail: str) -> HTTPException:
    return HTTPException(status_code=401, detail=detail, headers={"WWW-Authenticate": "Bearer"})


def authenticate(request: Request) -> User | None:
    """App-wide dependency: runs on every route. Returns None only on a public path."""
    if request.url.path in PUBLIC_PATHS | FRONTEND_PATHS:
        return None
    scheme, _, token = request.headers.get("Authorization", "").partition(" ")
    if scheme.lower() != "bearer" or not token.strip():
        raise _unauthorized("Missing bearer token")
    verifier: TokenVerifier = request.app.state.token_verifier
    try:
        return verifier.verify(token.strip())
    except InvalidTokenError as exc:
        raise _unauthorized("Invalid token") from exc
    except CertsUnavailableError as exc:
        raise HTTPException(status_code=503, detail="Cannot check the token now") from exc


def current_user(user: Annotated[User | None, Depends(authenticate)]) -> User:
    """For handlers that need the user. FastAPI reuses the app-wide check: one per request."""
    if user is None:
        raise _unauthorized("Missing bearer token")
    return user
