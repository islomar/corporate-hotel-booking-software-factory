from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from chb.app import create_app


@pytest.fixture
def static_dir(tmp_path: Path) -> Path:
    (tmp_path / "index.html").write_text("<h1>frontend</h1>")
    swagger = tmp_path / "swagger-ui"
    swagger.mkdir()
    (swagger / "swagger-ui-bundle.js").write_text("// bundle")
    (swagger / "swagger-ui.css").write_text("/* css */")
    return tmp_path


def csp_directives(csp: str) -> dict[str, str]:
    directives: dict[str, str] = {}
    for part in csp.split(";"):
        name, _, value = part.strip().partition(" ")
        directives[name] = value
    return directives


PATHS = [
    "/api/health",
    "/api/openapi.json",
    "/api/docs",
    "/api/docs-assets/swagger-ui-bundle.js",
    "/",
    "/index.html",
    "/nope",
    "/api/nope",
]


@pytest.mark.parametrize("path", PATHS)
def test_every_response_carries_the_security_headers(static_dir: Path, path: str) -> None:
    client = TestClient(create_app(static_dir=static_dir))

    response = client.get(path)

    csp = csp_directives(response.headers["content-security-policy"])
    assert csp["default-src"] == "'self'"
    assert csp["frame-ancestors"] == "'none'"
    assert csp["object-src"] == "'none'"
    assert csp["base-uri"] == "'self'"
    hsts = response.headers["strict-transport-security"]
    assert int(hsts.removeprefix("max-age=").split(";")[0]) >= 31_536_000
    assert response.headers["x-content-type-options"] == "nosniff"
    assert response.headers["referrer-policy"] == "strict-origin-when-cross-origin"


def test_a_405_response_carries_the_headers() -> None:
    client = TestClient(create_app())

    response = client.post("/api/health")

    assert response.status_code == 405
    assert "content-security-policy" in response.headers


def test_the_csp_names_no_other_origin_and_no_unsafe_script() -> None:
    client = TestClient(create_app())

    for path in ("/api/health", "/api/docs"):
        csp = client.get(path).headers["content-security-policy"]
        assert "http" not in csp
        assert "unsafe" not in csp_directives(csp).get("script-src", "")


def test_swagger_ui_page_loads_only_own_assets_and_no_inline_script() -> None:
    client = TestClient(create_app())

    html = client.get("/api/docs").text

    assert 'src="/api/docs-assets/swagger-ui-bundle.js"' in html
    assert 'href="/api/docs-assets/swagger-ui.css"' in html
    assert 'src="/api/docs-assets/init.js"' in html
    assert "cdn" not in html
    assert "jsdelivr" not in html
    assert "<script>" not in html


def test_swagger_ui_init_script_points_at_the_schema() -> None:
    client = TestClient(create_app())

    response = client.get("/api/docs-assets/init.js")

    assert response.status_code == 200
    assert "javascript" in response.headers["content-type"]
    assert "/api/openapi.json" in response.text


def test_swagger_ui_assets_are_served_from_the_static_dir(static_dir: Path) -> None:
    client = TestClient(create_app(static_dir=static_dir))

    bundle = client.get("/api/docs-assets/swagger-ui-bundle.js")

    assert bundle.status_code == 200
    assert bundle.text == "// bundle"


def test_redoc_is_off_because_it_loads_from_a_cdn() -> None:
    client = TestClient(create_app())

    assert client.get("/api/redoc").status_code == 404
