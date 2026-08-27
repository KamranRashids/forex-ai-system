"""Shared application-wide constants."""

from __future__ import annotations

APP_NAME: str = "forex-ai-api"
APP_VERSION: str = "0.1.0"

#: The only trading mode this system supports (SAFE MODE — see docs/safe-mode.md).
SAFE_TRADING_MODE: str = "safe"

API_V1_PREFIX: str = "/api/v1"

TOKEN_TYPE_ACCESS: str = "access"
TOKEN_TYPE_REFRESH: str = "refresh"

REFRESH_COOKIE_NAME: str = "refresh_token"

JWT_ALGORITHM: str = "HS256"


class AuditActions:
    """Canonical audit_log.action values (append-only vocabulary)."""

    USER_REGISTERED = "auth.user_registered"
    LOGIN_SUCCEEDED = "auth.login_succeeded"
    LOGIN_FAILED = "auth.login_failed"
    TOKEN_REFRESHED = "auth.token_refreshed"
    TOKEN_REUSE_DETECTED = "auth.token_reuse_detected"
    LOGGED_OUT = "auth.logged_out"
    USER_UPDATED = "admin.user_updated"
    MARKET_CONFIG_UPDATED = "admin.market_config_updated"
    BACKFILL_TRIGGERED = "admin.backfill_triggered"
    RISK_PARAMS_UPDATED = "admin.risk_params_updated"
    DECISION_REPLAY_TRIGGERED = "admin.decision_replay_triggered"
