"""JSON log lines on stdout, in the shape Cloud Logging reads.

Cloud Run sends stdout to Cloud Logging. A JSON line becomes a structured entry: `severity`
sets the level, `message` the text, and the `logging.googleapis.com/*` fields link the entry
to its Cloud Trace span. See https://cloud.google.com/logging/docs/structured-logging.
"""

import json
import logging
import sys
from datetime import UTC, datetime

from opentelemetry import trace

from chb.telemetry import HEALTH_PATH

_UVICORN_LOGGERS = ("uvicorn", "uvicorn.error", "uvicorn.access")


class DropHealthCheckAccessLines(logging.Filter):
    """Drops uvicorn's access line for `GET /api/health`. Every other request still logs.

    Health checks run every few seconds and would fill the logs. Their request metric is
    recorded by the OpenTelemetry instrumentation, which does not read these lines.
    """

    def filter(self, record: logging.LogRecord) -> bool:
        # uvicorn logs '%s - "%s %s HTTP/%s" %d' with (client, method, path?query, version, status).
        args = record.args
        if not isinstance(args, tuple) or len(args) < 3:
            return True
        method, path = args[1], args[2]
        return not (method == "GET" and str(path).split("?", 1)[0] == HEALTH_PATH)


class JsonFormatter(logging.Formatter):
    def __init__(self, project_id: str | None) -> None:
        super().__init__()
        self._project_id = project_id

    def format(self, record: logging.LogRecord) -> str:
        entry: dict[str, object] = {
            "severity": record.levelname,
            "message": record.getMessage(),
            "logger": record.name,
            "time": datetime.fromtimestamp(record.created, UTC).isoformat(),
        }
        if record.exc_info:
            entry["message"] = f"{entry['message']}\n{self.formatException(record.exc_info)}"
        context = trace.get_current_span().get_span_context()
        if context.is_valid:
            trace_id = format(context.trace_id, "032x")
            span_id = format(context.span_id, "016x")
            # Plain IDs: the local Grafana links logs to traces with these.
            entry["trace_id"] = trace_id
            entry["span_id"] = span_id
            if self._project_id:
                entry["logging.googleapis.com/trace"] = (
                    f"projects/{self._project_id}/traces/{trace_id}"
                )
            entry["logging.googleapis.com/spanId"] = span_id
            entry["logging.googleapis.com/trace_sampled"] = context.trace_flags.sampled
        return json.dumps(entry, default=str)


def configure_logging(project_id: str | None, extra_handlers: list[logging.Handler]) -> None:
    """One JSON handler on the root logger. Uvicorn's loggers propagate to it."""
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(JsonFormatter(project_id))
    root = logging.getLogger()
    root.handlers = [handler, *extra_handlers]
    root.setLevel(logging.INFO)
    for name in _UVICORN_LOGGERS:
        logger = logging.getLogger(name)
        logger.handlers = []
        logger.propagate = True
    # On the logger, not a handler: the stdout line and the OTLP export both lose it.
    logging.getLogger("uvicorn.access").filters = [DropHealthCheckAccessLines()]
