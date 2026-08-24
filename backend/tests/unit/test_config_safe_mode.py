"""SAFE MODE regression: configuration layer (L1) rejects every non-safe mode."""

from __future__ import annotations

import pytest
from app.core.config import Settings, validate_trading_mode
from pydantic import ValidationError


@pytest.mark.safety
@pytest.mark.unit
@pytest.mark.parametrize("bad_mode", ["live", "real", "paper", "LIVE", "Live", "", "   ", None])
def test_settings_reject_every_non_safe_mode(bad_mode: str | None) -> None:
    with pytest.raises(ValidationError, match="Refusing to start"):
        Settings(trading_mode=bad_mode)


@pytest.mark.safety
@pytest.mark.unit
def test_settings_accepts_and_normalizes_safe() -> None:
    settings = Settings(trading_mode="  SAFE ")
    assert settings.trading_mode == "safe"


@pytest.mark.safety
@pytest.mark.unit
def test_validate_trading_mode_rejects_non_safe() -> None:
    with pytest.raises(ValueError, match="live order execution does not exist"):
        validate_trading_mode("live")


@pytest.mark.safety
@pytest.mark.unit
def test_validate_trading_mode_accepts_case_insensitive_safe() -> None:
    assert validate_trading_mode("SAFE") == "safe"


@pytest.mark.safety
@pytest.mark.unit
def test_validate_trading_mode_rejects_missing_value() -> None:
    with pytest.raises(ValueError):
        validate_trading_mode(None)


@pytest.mark.safety
@pytest.mark.unit
def test_prod_env_rejects_insecure_default_secret() -> None:
    with pytest.raises(ValidationError, match="SECRET_KEY"):
        Settings(app_env="prod", secret_key="dev-only-insecure-secret-key-change-me-0123456789")


@pytest.mark.unit
def test_short_secret_rejected() -> None:
    with pytest.raises(ValidationError, match="at least 32 character"):
        Settings(secret_key="too-short")


@pytest.mark.unit
def test_invalid_log_level_rejected() -> None:
    with pytest.raises(ValidationError, match="LOG_LEVEL"):
        Settings(log_level="chatty")


@pytest.mark.unit
def test_get_settings_is_cached_and_resettable(monkeypatch: pytest.MonkeyPatch) -> None:
    from app.core.config import get_settings, reset_settings_cache

    first = get_settings()
    assert get_settings() is first
    monkeypatch.setenv("ACCESS_TOKEN_EXPIRE_MINUTES", "77")
    reset_settings_cache()
    second = get_settings()
    assert second is not first
    assert second.access_token_expire_minutes == 77


@pytest.mark.unit
def test_cookie_secure_only_in_prod() -> None:
    assert Settings(app_env="dev").cookie_secure is False
    assert Settings(app_env="prod", secret_key="p" * 40).cookie_secure is True
