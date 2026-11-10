import json
import logging

import pytest

from chb.logs import configure_logging


def log_access(method: str, path: str) -> None:
    """The call uvicorn makes for each response (uvicorn/protocols/http/h11_impl.py)."""
    logging.getLogger("uvicorn.access").info(
        '%s - "%s %s HTTP/%s" %d', "127.0.0.1:50000", method, path, "1.1", 200
    )


def access_messages(capsys: pytest.CaptureFixture[str]) -> list[str]:
    return [json.loads(line)["message"] for line in capsys.readouterr().out.splitlines()]


def test_health_check_access_lines_are_dropped(capsys: pytest.CaptureFixture[str]) -> None:
    configure_logging(None, [])

    log_access("GET", "/api/health")
    log_access("GET", "/api/health?probe=1")

    assert access_messages(capsys) == []


def test_other_access_lines_are_kept(capsys: pytest.CaptureFixture[str]) -> None:
    configure_logging(None, [])

    log_access("GET", "/api/docs")
    log_access("POST", "/api/health")
    log_access("GET", "/api/healthz")

    assert access_messages(capsys) == [
        '127.0.0.1:50000 - "GET /api/docs HTTP/1.1" 200',
        '127.0.0.1:50000 - "POST /api/health HTTP/1.1" 200',
        '127.0.0.1:50000 - "GET /api/healthz HTTP/1.1" 200',
    ]


def test_health_check_lines_do_not_reach_extra_handlers() -> None:
    records: list[logging.LogRecord] = []

    class Collect(logging.Handler):
        def emit(self, record: logging.LogRecord) -> None:
            records.append(record)

    configure_logging(None, [Collect()])

    log_access("GET", "/api/health")
    log_access("GET", "/api/docs")

    assert [record.args[2] for record in records if isinstance(record.args, tuple)] == ["/api/docs"]


def test_configuring_twice_keeps_one_filter() -> None:
    configure_logging(None, [])
    configure_logging(None, [])

    assert len(logging.getLogger("uvicorn.access").filters) == 1
