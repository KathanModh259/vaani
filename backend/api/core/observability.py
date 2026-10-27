"""Logging format and error tracking.

LOG_FORMAT=json emits one JSON object per line - what Loki, CloudWatch,
Datadog and GCP logging parse without regexes. Every record carries the
request id set by RequestLoggingMiddleware, so a failing request can be
followed across its log lines.

SENTRY_DSN turns on Sentry. Transcripts and caller details are personal
data: send_default_pii stays off and request bodies are not attached.
"""

import json
import logging
from datetime import datetime, timezone

from scripts.config import settings


class _RequestIdFilter(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        from api.core.middleware import request_id_var
        record.request_id = request_id_var.get()
        return True


class _JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        out = {
            "ts": datetime.fromtimestamp(record.created, timezone.utc).isoformat(),
            "level": record.levelname,
            "logger": record.name,
            "msg": record.getMessage(),
            "request_id": getattr(record, "request_id", "-"),
        }
        if record.exc_info:
            out["exc"] = self.formatException(record.exc_info)
        return json.dumps(out, ensure_ascii=False)


def configure_logging() -> None:
    root = logging.getLogger()
    if getattr(root, "_vaani_configured", False):
        return
    handler = logging.StreamHandler()
    handler.addFilter(_RequestIdFilter())
    if settings.LOG_FORMAT == "json":
        handler.setFormatter(_JsonFormatter())
    else:
        handler.setFormatter(logging.Formatter("%(asctime)s - %(name)s - %(levelname)s - %(message)s"))
    root.handlers = [handler]
    root.setLevel(getattr(logging, settings.LOG_LEVEL.upper(), logging.INFO))
    root._vaani_configured = True


def init_sentry() -> bool:
    if not settings.SENTRY_DSN:
        return False
    try:
        import sentry_sdk
    except ImportError:
        logging.getLogger(__name__).warning("SENTRY_DSN set but sentry-sdk is not installed")
        return False
    sentry_sdk.init(
        dsn=settings.SENTRY_DSN,
        environment=settings.ENVIRONMENT,
        send_default_pii=False,
        max_request_body_size="never",
        traces_sample_rate=0.0,
    )
    return True
