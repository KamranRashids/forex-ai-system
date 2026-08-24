"""Shared test configuration: deterministic environment + settings cache reset."""

from __future__ import annotations

from collections.abc import Iterator

import pytest


@pytest.fixture(autouse=True)
def _deterministic_env(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """Pin core env so tests are hermetic regardless of host/.env state."""
    from app.core.config import reset_settings_cache

    monkeypatch.setenv("TRADING_MODE", "safe")
    monkeypatch.setenv("APP_ENV", "dev")
    monkeypatch.delenv("SENTRY_DSN", raising=False)
    reset_settings_cache()
    yield
    reset_settings_cache()


@pytest.fixture()
def secret_key() -> str:
    return "unit-test-secret-key-0123456789abcdef0123456789abcdef"


@pytest.fixture()
def test_settings(secret_key: str):
    from app.core.config import Settings

    return Settings(secret_key=secret_key)
