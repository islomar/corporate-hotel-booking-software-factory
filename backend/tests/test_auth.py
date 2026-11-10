import time
from collections.abc import Mapping

import pytest
from conftest import TokenIssuer

from chb.auth import (
    CachedCerts,
    CertsUnavailableError,
    FirebaseTokenVerifier,
    InvalidTokenError,
    RejectAll,
    User,
    max_age_seconds,
)


class Clock:
    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now


class CountingFetch:
    def __init__(self, max_age: float) -> None:
        self.calls = 0
        self.max_age = max_age

    def __call__(self) -> tuple[Mapping[str, str], float]:
        self.calls += 1
        return {"k": f"cert-{self.calls}"}, self.max_age


def test_certs_are_fetched_once_while_fresh() -> None:
    fetch, clock = CountingFetch(max_age=60), Clock()
    certs = CachedCerts(fetch, clock)

    first = certs()
    clock.now += 59
    second = certs()

    assert first == second == {"k": "cert-1"}
    assert fetch.calls == 1


def test_certs_are_fetched_again_when_stale() -> None:
    fetch, clock = CountingFetch(max_age=60), Clock()
    certs = CachedCerts(fetch, clock)

    certs()
    clock.now += 60

    assert certs() == {"k": "cert-2"}


def test_certs_without_max_age_are_not_cached() -> None:
    fetch = CountingFetch(max_age=0)
    certs = CachedCerts(fetch, Clock())

    certs()
    certs()

    assert fetch.calls == 2


@pytest.mark.parametrize(
    ("header", "expected"),
    [
        ("public, max-age=19302, must-revalidate, no-transform", 19302.0),
        ("max-age=60", 60.0),
        ("no-cache", 0.0),
        ("", 0.0),
        ("max-age=abc", 0.0),
    ],
)
def test_max_age_comes_from_cache_control(header: str, expected: float) -> None:
    assert max_age_seconds(header) == expected


def verifier(issuer: TokenIssuer) -> FirebaseTokenVerifier:
    return FirebaseTokenVerifier(issuer.project_id, CachedCerts(issuer.fetch_certs))


def test_verify_returns_the_user(issuer: TokenIssuer) -> None:
    assert verifier(issuer).verify(issuer.token()) == User(uid="user-123", email="ada@example.com")


def test_verify_accepts_a_user_without_email(issuer: TokenIssuer) -> None:
    assert verifier(issuer).verify(issuer.token(email=None)) == User(uid="user-123", email=None)


@pytest.mark.parametrize("sub", [None, "", 42])
def test_verify_rejects_a_token_without_a_subject(issuer: TokenIssuer, sub: object) -> None:
    with pytest.raises(InvalidTokenError):
        verifier(issuer).verify(issuer.token(sub=sub))


def test_verify_rejects_a_sign_in_in_the_future(issuer: TokenIssuer) -> None:
    token = issuer.token(auth_time=10**10)

    with pytest.raises(InvalidTokenError):
        verifier(issuer).verify(token)


def test_verify_rejects_a_token_without_auth_time(issuer: TokenIssuer) -> None:
    with pytest.raises(InvalidTokenError):
        verifier(issuer).verify(issuer.token(auth_time=None))


def test_verify_rejects_garbage(issuer: TokenIssuer) -> None:
    with pytest.raises(InvalidTokenError):
        verifier(issuer).verify("a.b.c")


def test_verify_reports_a_cert_fetch_failure_apart_from_bad_tokens(issuer: TokenIssuer) -> None:
    def failing() -> tuple[Mapping[str, str], float]:
        raise OSError("network down")

    with pytest.raises(CertsUnavailableError):
        FirebaseTokenVerifier(issuer.project_id, CachedCerts(failing)).verify(issuer.token())


def test_reject_all_rejects_every_token(issuer: TokenIssuer) -> None:
    with pytest.raises(InvalidTokenError):
        RejectAll().verify(issuer.token())


def test_verify_tolerates_a_little_clock_skew(issuer: TokenIssuer) -> None:
    soon = int(time.time()) + 5

    user = verifier(issuer).verify(issuer.token(iat=soon, auth_time=soon))

    assert user.uid == "user-123"


@pytest.mark.parametrize("claim", ["iat", "auth_time"])
def test_verify_rejects_a_token_from_the_future(issuer: TokenIssuer, claim: str) -> None:
    later = int(time.time()) + 60

    with pytest.raises(InvalidTokenError):
        verifier(issuer).verify(issuer.token(**{claim: later}))
