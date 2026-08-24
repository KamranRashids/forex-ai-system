"""FastAPI dependencies: DB session, settings, auth, RBAC, rate limiting."""

from __future__ import annotations

import uuid
from typing import Annotated

from fastapi import Depends, Request
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import Settings, get_settings
from app.core.constants import TOKEN_TYPE_ACCESS
from app.core.errors import AuthenticationError, PermissionDeniedError, RateLimitError
from app.core.ratelimit import SlidingWindowLimiter
from app.core.security import decode_token, role_at_least
from app.db.session import get_db
from app.models.user import User

_bearer_scheme = HTTPBearer(auto_error=False)

#: Process-wide limiter for auth endpoints (per-process by design; see ratelimit.py).
auth_limiter: SlidingWindowLimiter = SlidingWindowLimiter()

DBSession = Annotated[AsyncSession, Depends(get_db)]
SettingsDep = Annotated[Settings, Depends(get_settings)]


async def get_current_user(
    credentials: Annotated[HTTPAuthorizationCredentials | None, Depends(_bearer_scheme)],
    session: DBSession,
    settings: SettingsDep,
) -> User:
    """Resolve the caller from a Bearer access token."""
    if credentials is None:
        raise AuthenticationError("Missing bearer token")

    payload = decode_token(
        credentials.credentials, expected_type=TOKEN_TYPE_ACCESS, settings=settings
    )
    try:
        user_id = uuid.UUID(str(payload.get("sub")))
    except ValueError as exc:
        raise AuthenticationError("Invalid token subject") from exc

    user = await session.get(User, user_id)
    if user is None or not user.is_active:
        raise AuthenticationError("Unknown or disabled account")
    return user


def require_role(required: str) -> object:
    """Dependency factory enforcing the viewer < trader < admin hierarchy."""

    async def _dependency(user: Annotated[User, Depends(get_current_user)]) -> User:
        if not role_at_least(user.role.value, required):
            raise PermissionDeniedError(f"Requires role {required!r} or higher")
        return user

    return _dependency


CurrentUser = Annotated[User, Depends(get_current_user)]
AdminUser = Annotated[User, Depends(require_role("admin"))]


def client_ip(request: Request) -> str | None:
    """Best-effort client IP (direct peer; proxy handling lands with nginx)."""
    return request.client.host if request.client else None


def enforce_rate_limit(scope: str) -> object:
    """Dependency factory applying a per-IP sliding window for `scope`."""

    async def _dependency(request: Request, settings: SettingsDep) -> None:
        limit_by_scope = {
            "login": settings.rate_limit_login_per_minute,
            "register": settings.rate_limit_register_per_minute,
            "refresh": settings.rate_limit_refresh_per_minute,
        }
        limit = limit_by_scope.get(scope)
        if limit is None:
            return
        ip = client_ip(request) or "unknown"
        result = auth_limiter.check(f"{scope}:{ip}", limit=limit)
        if not result.allowed:
            raise RateLimitError(result.retry_after_seconds)

    return _dependency
