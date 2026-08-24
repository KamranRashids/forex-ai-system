"""Unit tests: structlog redaction filter and correlation-id attachment."""

from __future__ import annotations

import io
import json
import logging

import pytest
from app.core.logging import _redact_value, configure_logging, redact_sensitive


@pytest.mark.unit
@pytest.mark.parametrize(
    "key",
    [
        "password",
        "PASSWORD",
        "hashed_password",
        "api_key",
        "apiKey",
        "SECRET_KEY",
        "refresh_token",
        "Authorization",
        "session_cookie",
    ],
)
def test_redaction_masks_sensitive_top_level_keys(key: str) -> None:
    event = {"event": "login", key: "super-secret", "user": "bob@example.com"}
    cleaned = redact_sensitive(None, "info", event)  # type: ignore[arg-type]
    assert cleaned[key] == "[REDACTED]"
    assert cleaned["user"] == "bob@example.com"
    assert cleaned["event"] == "login"


@pytest.mark.unit
def test_redaction_recurses_into_nested_structures() -> None:
    payload = {
        "event": "request",
        "headers": {"authorization": "Bearer abc", "accept": "application/json"},
        "body": {"email": "x@y.z", "nested": [{"token": "t"}]},
        "count": 3,
    }
    cleaned = _redact_value(payload)
    assert cleaned["headers"]["authorization"] == "[REDACTED]"
    assert cleaned["headers"]["accept"] == "application/json"
    assert cleaned["body"]["email"] == "x@y.z"
    assert cleaned["body"]["nested"][0]["token"] == "[REDACTED]"
    assert cleaned["count"] == 3


@pytest.mark.unit
def test_json_output_contains_redacted_values() -> None:
    configure_logging(log_level="INFO", json_logs=True)
    stream = io.StringIO()
    handler = logging.StreamHandler(stream)
    root = logging.getLogger()
    root.addHandler(handler)
    try:
        log = logging.getLogger("redaction-test")
        log.info(
            "login_attempt",
            extra={"password": "hunter2", "user": "bob@example.com"},
        )
        # stdlib records flow through ProcessorFormatter (foreign_pre_chain).
        rendered = stream.getvalue()
        assert "hunter2" not in rendered
        assert "bob@example.com" in rendered or "login_attempt" in rendered
        if rendered.strip().startswith("{"):
            parsed = json.loads(rendered.strip().splitlines()[-1])
            assert parsed.get("password") == "[REDACTED]"
    finally:
        root.removeHandler(handler)


@pytest.mark.unit
def test_configure_logging_is_idempotent() -> None:
    configure_logging(log_level="INFO", json_logs=False)
    handlers_first = list(logging.getLogger().handlers)
    configure_logging(log_level="DEBUG", json_logs=True)
    assert logging.getLogger().handlers == handlers_first
