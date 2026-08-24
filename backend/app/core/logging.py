"""Structured logging: structlog JSON/console output with correlation IDs.

- Every request carries a correlation id (``asgi-correlation-id`` middleware);
  it is attached to every log record and returned to clients in the
  ``X-Request-ID`` response header and problem+json bodies.
- A redaction processor scrubs sensitive values (passwords, tokens, secrets,
  API keys, cookies) from event payloads before rendering.
- Dev renders pretty console lines; prod renders JSON (machine-parseable).
"""

from __future__ import annotations

import logging
import sys
from typing import Any

import structlog
from asgi_correlation_id import correlation_id
from structlog.processors import StackInfoRenderer, TimeStamper, add_log_level
from structlog.stdlib import ProcessorFormatter
from structlog.types import EventDict, Processor

_REDACTED: str = "[REDACTED]"

#: Lowercased substrings; any matching dict key has its value redacted.
SENSITIVE_KEY_SUBSTRINGS: tuple[str, ...] = (
    "password",
    "passwd",
    "secret",
    "token",
    "authorization",
    "api_key",
    "apikey",
    "cookie",
    "credential",
)

_MAX_REDACTION_DEPTH: int = 6


def _is_sensitive_key(key: str) -> bool:
    lowered = key.lower()
    return any(marker in lowered for marker in SENSITIVE_KEY_SUBSTRINGS)


def _redact_value(value: Any, depth: int = 0) -> Any:
    if depth > _MAX_REDACTION_DEPTH:
        return value
    if isinstance(value, dict):
        return {
            key: _REDACTED
            if isinstance(key, str) and _is_sensitive_key(key)
            else _redact_value(val, depth + 1)
            for key, val in value.items()
        }
    if isinstance(value, (list, tuple)):
        redacted_items = [_redact_value(item, depth + 1) for item in value]
        return type(value)(redacted_items)
    return value


def redact_sensitive(logger: Any, method_name: str, event_dict: EventDict) -> EventDict:
    """Structlog processor that masks sensitive values in the event payload."""
    for key in list(event_dict.keys()):
        if key != "event" and _is_sensitive_key(key):
            event_dict[key] = _REDACTED
        else:
            event_dict[key] = _redact_value(event_dict[key])
    return event_dict


def add_correlation_id(logger: Any, method_name: str, event_dict: EventDict) -> EventDict:
    """Attach the current request correlation id, when inside a request."""
    cid = correlation_id.get(None)
    if cid is not None:
        event_dict["correlation_id"] = cid
    return event_dict


_shared_processors: list[Processor] = [
    structlog.contextvars.merge_contextvars,
    add_correlation_id,
    add_log_level,
    TimeStamper(fmt="iso", utc=True),
    StackInfoRenderer(),
    structlog.dev.set_exc_info,
    redact_sensitive,
]

_configured: bool = False


class _CurrentStderrHandler(logging.StreamHandler):  # type: ignore[type-arg]
    """StreamHandler that always writes to the *current* sys.stderr.

    Test runners and process supervisors swap sys.stderr at runtime; binding
    the stream once at configuration time eventually writes into a closed
    capture buffer. Resolving per-emit avoids that entire failure class.
    """

    def __init__(self) -> None:
        super().__init__(sys.stderr)

    @property
    def stream(self) -> Any:
        return sys.stderr

    @stream.setter
    def stream(self, value: Any) -> None:  # noqa: ARG002 - signature required by base
        pass


def configure_logging(log_level: str = "INFO", json_logs: bool = False) -> None:
    """Configure structlog + stdlib logging. Safe to call multiple times."""
    global _configured
    if _configured:
        return

    renderer: Processor = (
        structlog.processors.JSONRenderer() if json_logs else structlog.dev.ConsoleRenderer()
    )
    formatter = ProcessorFormatter(
        foreign_pre_chain=_shared_processors,
        processors=[
            ProcessorFormatter.remove_processors_meta,
            renderer,
        ],
    )

    handler = _CurrentStderrHandler()
    handler.setFormatter(formatter)

    root = logging.getLogger()
    root.handlers.clear()
    root.addHandler(handler)
    root.setLevel(log_level.upper())

    structlog.configure(
        processors=[*_shared_processors, structlog.stdlib.ProcessorFormatter.wrap_for_formatter],
        logger_factory=structlog.stdlib.LoggerFactory(),
        wrapper_class=structlog.stdlib.BoundLogger,
        cache_logger_on_first_use=True,
    )

    # Uvicorn installs its own handlers/log formats; route them through structlog too.
    for noisy_logger in ("uvicorn", "uvicorn.error", "uvicorn.access"):
        std_logger = logging.getLogger(noisy_logger)
        std_logger.handlers.clear()
        std_logger.propagate = True

    _configured = True
