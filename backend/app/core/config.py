"""Typed application settings with SAFE MODE enforcement (layer L1).

The configuration layer itself refuses to produce a :class:`Settings` object
whose ``trading_mode`` is anything other than ``"safe"``. There is no bypass:
live order execution does not exist anywhere in this codebase (see
``docs/safe-mode.md``). An invalid value aborts startup — this is intentional
and covered by the SAFE MODE regression suite.
"""

from __future__ import annotations

from functools import lru_cache
from typing import Final, Literal

from pydantic import Field, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

from app.core.constants import SAFE_TRADING_MODE

ALLOWED_TRADING_MODES: Final[frozenset[str]] = frozenset({SAFE_TRADING_MODE})

#: Minimum length for SECRET_KEY (JWT signing material).
SECRET_KEY_MIN_LENGTH: Final[int] = 32


def validate_trading_mode(raw_trading_mode: str | None) -> str:
    """Normalize and validate the trading mode; return the canonical value.

    Raises:
        ValueError: for any mode other than ``safe`` — SAFE MODE layer L1.
    """
    mode = (raw_trading_mode or "").strip().lower()
    if mode not in ALLOWED_TRADING_MODES:
        raise ValueError(
            f"Refusing to start: TRADING_MODE={raw_trading_mode!r} is not permitted. "
            f"Only {sorted(ALLOWED_TRADING_MODES)!r} is allowed; "
            "live order execution does not exist."
        )
    return mode


class Settings(BaseSettings):
    """Process environment configuration (12-factor; compose/host/test friendly)."""

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore",
    )

    # --- Core / safety -------------------------------------------------------
    app_env: Literal["dev", "prod"] = "dev"
    trading_mode: str = SAFE_TRADING_MODE
    log_level: str = "INFO"
    secret_key: str = Field(
        default="dev-only-insecure-secret-key-change-me-0123456789abcdef",
        min_length=SECRET_KEY_MIN_LENGTH,
    )
    access_token_expire_minutes: int = Field(default=30, gt=0)
    refresh_token_expire_days: int = Field(default=14, gt=0)
    cors_origins: str = "http://localhost:3000"
    sentry_dsn: str = ""

    # --- Datastores ------------------------------------------------------------
    database_url: str = "postgresql+asyncpg://forex:change-me-dev-only@localhost:5432/forex_ai"
    redis_url: str = "redis://localhost:6379/0"

    # --- Auth rate limits (per client IP) --------------------------------------
    rate_limit_login_per_minute: int = Field(default=10, gt=0)
    rate_limit_register_per_minute: int = Field(default=5, gt=0)
    rate_limit_refresh_per_minute: int = Field(default=30, gt=0)

    @field_validator("trading_mode", mode="before")
    @classmethod
    def _enforce_safe_mode(cls, value: object) -> str:
        raw = value if isinstance(value, str) else None
        return validate_trading_mode(raw)

    @field_validator("log_level")
    @classmethod
    def _normalize_log_level(cls, value: str) -> str:
        level = value.strip().upper()
        allowed = {"DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"}
        if level not in allowed:
            raise ValueError(f"LOG_LEVEL must be one of {sorted(allowed)}; got {value!r}")
        return level

    @model_validator(mode="after")
    def _reject_insecure_prod_secret(self) -> Settings:
        if self.app_env == "prod" and self.secret_key.startswith("dev-only"):
            raise ValueError(
                "SECRET_KEY is an insecure development default; "
                "generate a strong random value before running with APP_ENV=prod."
            )
        return self

    @property
    def cors_origin_list(self) -> list[str]:
        return [origin.strip() for origin in self.cors_origins.split(",") if origin.strip()]

    @property
    def cookie_secure(self) -> bool:
        """Refresh cookies are Secure-only outside local development."""
        return self.app_env == "prod"

    @property
    def json_logs(self) -> bool:
        return self.app_env == "prod"


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """Return the cached process-wide settings instance."""
    return Settings()


def reset_settings_cache() -> None:
    """Clear the cached settings (used by tests that mutate environment)."""
    get_settings.cache_clear()
