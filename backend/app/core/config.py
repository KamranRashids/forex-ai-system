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

    # --- Market data (Phase 2) ---------------------------------------------------
    #: Provider-independent ingestion (ADR-0003). Only "synthetic" needs no
    #: credentials; "oanda" requires OANDA_API_TOKEN and practice-only env.
    market_data_provider: str = "synthetic"
    market_data_symbols: str = "EURUSD,GBPUSD,USDJPY,AUDUSD,USDCAD,USDCHF,NZDUSD"
    #: Any of M5,M15,H1,H4,D1 (engine is generic; ADR-0003 default starts M15/H1/H4).
    market_data_timeframes: str = "M15,H1,H4"
    oanda_api_token: str = ""
    oanda_env: str = "practice"  # only "practice" is permitted in v1
    ingest_interval_seconds: int = Field(default=10, ge=1)
    ingest_max_bars_per_cycle: int = Field(default=500, ge=1)
    ingest_initial_history_days: int = Field(default=2, ge=0)
    provider_breaker_failure_threshold: int = Field(default=5, ge=1)
    provider_breaker_cooldown_seconds: int = Field(default=60, ge=1)
    staleness_poll_seconds: int = Field(default=30, ge=1)

    # --- News & economic calendar (Phase 4) ---------------------------------------
    #: news_provider/calendar_provider accept "synthetic" (default, zero-key,
    #: deterministic demo data) or "finnhub" (requires FINNHUB_API_TOKEN).
    news_provider: str = "synthetic"
    calendar_provider: str = "synthetic"
    finnhub_api_token: str = ""
    #: How often the poller fetches + persists normalized news/calendar data.
    news_poll_seconds: int = Field(default=300, ge=5)
    calendar_poll_seconds: int = Field(default=3600, ge=60)
    #: Look-back window (hours) requested from providers on each poll.
    news_lookback_hours: int = Field(default=24, ge=1)
    calendar_lookback_hours: int = Field(default=48, ge=1)
    #: Composite dedup window enforced by persistence (replays are idempotent
    #: regardless; this bounds un-hashed provider records).
    news_dedup_days: int = Field(default=7, ge=1)
    calendar_dedup_days: int = Field(default=7, ge=1)

    # --- LLM (Phase 4; ADR-0004: OpenCode Zen / Ox Alpha Free) ----------------------
    #: Abstraced behind LLMClient; agents NEVER call an LLM provider directly.
    llm_provider: str = "none"  # "none" | "opencode_zen"
    opencode_zen_api_key: str = ""
    opencode_zen_model: str = "ox-alpha-free"
    opencode_zen_base_url: str = "https://opencode.ai/api"
    #: Hard daily cost ceiling (USD); the budget breaker stops calls once hit.
    llm_daily_budget_usd: float = Field(default=0.0, ge=0.0)
    llm_max_tokens: int = Field(default=512, ge=16)
    llm_timeout_seconds: float = Field(default=30.0, gt=0.0)

    # --- Orchestrator (Phase 5) ------------------------------------------------
    #: Orchestrator loop cadence (ms).
    orch_poll_ms: int = Field(default=200, ge=50)
    #: Fraction of expected agents that must contribute a fresh vote; below
    #: this the orchestrator must not emit actionable output (fail closed).
    orch_min_agent_coverage: float = Field(default=0.5, ge=0.0, le=1.0)
    #: Minimum weighted |score| to go LONG/SHORT (below -> FLAT / ANALYSIS).
    orch_fusion_threshold: float = Field(default=0.15, ge=0.0, le=1.0)
    #: Minimum agreement fraction to allow PAPER intent (else ANALYSIS).
    orch_agreement_min: float = Field(default=0.5, ge=0.0, le=1.0)
    #: Hysteresis band (score shift required to flip direction).
    orch_hysteresis: float = Field(default=0.04, ge=0.0, le=1.0)
    #: Per-pair cooldown between PAPER decisions (seconds).
    orch_pair_cooldown_seconds: int = Field(default=1800, ge=0)

    # --- Risk agent (Phase 5) ----------------------------------------------------
    #: Max % of account equity risked per trade (0.01 = 1%).
    risk_max_risk_pct_account: float = Field(default=0.01, gt=0.0, le=1.0)
    #: Max aggregate open paper exposure fraction of account.
    risk_max_exposure_pct: float = Field(default=0.30, gt=0.0, le=1.0)
    #: Max daily realized loss fraction that stops new paper entries.
    risk_max_daily_loss_pct: float = Field(default=0.03, gt=0.0, le=1.0)
    #: Max drawdown fraction that stops new paper entries until recovery.
    risk_max_drawdown_pct: float = Field(default=0.10, gt=0.0, le=1.0)
    #: Minimum reward:risk required to accept a paper intent.
    risk_min_rr: float = Field(default=1.5, gt=0.0)
    #: Stop distance as a multiple of ATR(14).
    risk_sl_atr_multiple: float = Field(default=1.5, gt=0.0)
    #: Take-profit distance as a multiple of ATR(14).
    risk_tp_atr_multiple: float = Field(default=2.5, gt=0.0)
    #: Annualized volatility target (%) used to anchor position size.
    risk_vol_target_pct: float = Field(default=0.20, gt=0.0)
    #: Max exposure per correlated basket, as fraction of account.
    risk_correlation_cap_pct: float = Field(default=0.15, gt=0.0, le=1.0)
    #: Master switch for risk gating (off only for tests/diagnostics).
    risk_enabled: bool = Field(default=True)
    #: Notional account equity used for paper risk/sizing math.
    risk_paper_equity: float = Field(default=100_000.0, gt=0.0)

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

    @field_validator("market_data_provider")
    @classmethod
    def _validate_provider(cls, value: str) -> str:
        provider = value.strip().lower()
        allowed = {"synthetic", "oanda"}
        if provider not in allowed:
            raise ValueError(
                f"MARKET_DATA_PROVIDER must be one of {sorted(allowed)}; got {value!r}"
            )
        # Token presence is validated at adapter construction so tests can
        # build Settings freely without credentials.
        return provider

    @field_validator("market_data_timeframes")
    @classmethod
    def _validate_timeframes(cls, value: str) -> str:
        from app.data.timeframes import Timeframe

        raw = [part.strip().upper() for part in value.split(",") if part.strip()]
        if not raw:
            raise ValueError("MARKET_DATA_TIMEFRAMES must contain at least one timeframe")
        unknown = [tf for tf in raw if tf not in Timeframe.values()]
        if unknown:
            raise ValueError(
                f"Unknown timeframes {unknown}; supported: {sorted(Timeframe.values())}"
            )
        ordered = [tf for tf in Timeframe.values() if tf in raw]
        return ",".join(ordered)

    @field_validator("market_data_symbols")
    @classmethod
    def _validate_symbols(cls, value: str) -> str:
        symbols = [s.strip().upper() for s in value.split(",") if s.strip()]
        if not symbols:
            raise ValueError("MARKET_DATA_SYMBOLS must contain at least one pair")
        for symbol in symbols:
            if len(symbol) != 6 or not symbol.isalpha():
                raise ValueError(f"Invalid FX pair {symbol!r}; expected 6-letter form like EURUSD")
        return ",".join(symbols)

    @field_validator("news_provider", "calendar_provider")
    @classmethod
    def _validate_content_provider(cls, value: str) -> str:
        provider = value.strip().lower()
        allowed = {"synthetic", "finnhub"}
        if provider not in allowed:
            raise ValueError(f"content provider must be one of {sorted(allowed)}; got {value!r}")
        return provider

    @field_validator("llm_provider")
    @classmethod
    def _validate_llm_provider(cls, value: str) -> str:
        provider = value.strip().lower()
        allowed = {"none", "opencode_zen"}
        if provider not in allowed:
            raise ValueError(f"LLM_PROVIDER must be one of {sorted(allowed)}; got {value!r}")
        return provider

    @field_validator("oanda_env")
    @classmethod
    def _enforce_oanda_practice_only(cls, value: str) -> str:
        env = value.strip().lower()
        if env != "practice":
            raise ValueError(
                f'OANDA_ENV must be "practice"; got {value!r}. '
                "Live/real-money environments do not exist in this system (SAFE MODE)."
            )
        return env

    @property
    def market_symbols(self) -> list[str]:
        return [s.strip().upper() for s in self.market_data_symbols.split(",") if s.strip()]

    @property
    def market_timeframes(self) -> list[str]:
        return [tf.strip().upper() for tf in self.market_data_timeframes.split(",") if tf.strip()]

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
