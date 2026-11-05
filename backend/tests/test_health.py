from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from chb.app import create_app


def test_health_reports_ok_and_version(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("APP_VERSION", "abc123")
    client = TestClient(create_app())

    response = client.get("/api/health")

    assert response.status_code == 200
    assert response.json() == {"status": "ok", "version": "abc123"}


def test_serves_frontend_when_static_dir_exists(tmp_path: Path) -> None:
    (tmp_path / "index.html").write_text("<h1>frontend</h1>")
    client = TestClient(create_app(static_dir=tmp_path))

    response = client.get("/")

    assert response.status_code == 200
    assert "frontend" in response.text


def test_api_still_wins_over_static_files(tmp_path: Path) -> None:
    (tmp_path / "index.html").write_text("<h1>frontend</h1>")
    client = TestClient(create_app(static_dir=tmp_path))

    assert client.get("/api/health").json()["status"] == "ok"
