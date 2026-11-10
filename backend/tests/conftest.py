import datetime
import time
from collections.abc import Mapping
from typing import Any

import pytest
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.x509.oid import NameOID
from google.auth import crypt, jwt

PROJECT_ID = "chb-test-project"


class TokenIssuer:
    """Signs Firebase-shaped ID tokens with a key made for the test run, offline."""

    def __init__(self, key_id: str = "test-key") -> None:
        key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "securetoken-test")])
        now = datetime.datetime.now(datetime.UTC)
        cert = (
            x509.CertificateBuilder()
            .subject_name(name)
            .issuer_name(name)
            .public_key(key.public_key())
            .serial_number(x509.random_serial_number())
            .not_valid_before(now - datetime.timedelta(days=1))
            .not_valid_after(now + datetime.timedelta(days=1))
            .sign(key, hashes.SHA256())
        )
        private_pem = key.private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.PKCS8,
            serialization.NoEncryption(),
        )
        self.key_id = key_id
        self.project_id = PROJECT_ID
        self.certs = {key_id: cert.public_bytes(serialization.Encoding.PEM).decode()}
        self._signer = crypt.RSASigner.from_string(private_pem, key_id=key_id)  # pyright: ignore[reportUnknownMemberType]

    def token(self, **overrides: Any) -> str:
        now = int(time.time())
        claims: dict[str, Any] = {
            "iss": f"https://securetoken.google.com/{PROJECT_ID}",
            "aud": PROJECT_ID,
            "auth_time": now - 60,
            "sub": "user-123",
            "user_id": "user-123",
            "email": "ada@example.com",
            "iat": now - 60,
            "exp": now + 3600,
        }
        claims.update(overrides)
        claims = {k: v for k, v in claims.items() if v is not None}
        return jwt.encode(self._signer, claims).decode()  # pyright: ignore[reportUnknownMemberType]

    def fetch_certs(self) -> tuple[Mapping[str, str], float]:
        return self.certs, 3600.0


@pytest.fixture(scope="session")
def issuer() -> TokenIssuer:
    return TokenIssuer()


@pytest.fixture(scope="session")
def stranger() -> TokenIssuer:
    """Same key ID and claims as `issuer`, another private key."""
    return TokenIssuer()
