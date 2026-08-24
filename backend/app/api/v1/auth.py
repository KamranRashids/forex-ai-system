"""Authentication endpoints: register, login, refresh, logout, me.

Refresh tokens are delivered as ``HttpOnly`` cookies scoped to this router
(``/api/v1/auth``); access tokens live in memory on clients. Rotation and
reuse detection are handled by ``app/services/auth_service.py``.
"""

from __future__ import annotations

from datetime import timedelta
from typing import Annotated

from fastapi import APIRouter, Depends, Request, Response, status
from fastapi.security import OAuth2PasswordRequestForm

from app.api.deps import CurrentUser, DBSession, SettingsDep, client_ip, enforce_rate_limit
from app.core.config import Settings
from app.core.constants import API_V1_PREFIX, REFRESH_COOKIE_NAME
from app.core.errors import AuthenticationError
from app.schemas.auth import LogoutOut, RegisterIn, TokenOut, UserCreatedOut
from app.schemas.user import UserOut
from app.services.auth_service import (
    authenticate_user,
    issue_token_pair,
    register_user,
    revoke_refresh,
    rotate_refresh,
)

router = APIRouter(prefix="/auth", tags=["auth"])

_COOKIE_PATH: str = f"{API_V1_PREFIX}/auth"


def _set_refresh_cookie(
    response: Response, token: str, settings: Settings, expires_at_days: float
) -> None:
    response.set_cookie(
        key=REFRESH_COOKIE_NAME,
        value=token,
        max_age=int(timedelta(days=expires_at_days).total_seconds()),
        httponly=True,
        secure=settings.cookie_secure,
        samesite="lax",
        path=_COOKIE_PATH,
    )


@router.post("/register", status_code=status.HTTP_201_CREATED)
async def register(
    body: RegisterIn,
    session: DBSession,
    settings: SettingsDep,
    request: Request,
    _rate: Annotated[None, Depends(enforce_rate_limit("register"))],
) -> UserCreatedOut:
    """Create an account. The first registered user becomes admin."""
    user = await register_user(
        session,
        email=body.email,
        password=body.password,
        ip_address=client_ip(request),
    )
    return UserCreatedOut.model_validate(user)


@router.post("/login")
async def login(
    response: Response,
    session: DBSession,
    settings: SettingsDep,
    request: Request,
    form: Annotated[OAuth2PasswordRequestForm, Depends()],
    _rate: Annotated[None, Depends(enforce_rate_limit("login"))],
) -> TokenOut:
    """OAuth2 password-flow login; sets the refresh cookie."""
    user = await authenticate_user(
        session,
        email=form.username,
        password=form.password,
        ip_address=client_ip(request),
    )
    pair = await issue_token_pair(
        session,
        user=user,
        settings=settings,
        ip_address=client_ip(request),
        user_agent=request.headers.get("user-agent"),
    )
    _set_refresh_cookie(response, pair.refresh_token, settings, settings.refresh_token_expire_days)
    return TokenOut(access_token=pair.access_token, expires_at=pair.access_expires_at)


@router.post("/refresh")
async def refresh(
    request: Request,
    response: Response,
    session: DBSession,
    settings: SettingsDep,
    _rate: Annotated[None, Depends(enforce_rate_limit("refresh"))],
) -> TokenOut:
    """Rotate the refresh cookie into a fresh access+refresh pair."""
    raw = request.cookies.get(REFRESH_COOKIE_NAME)
    if not raw:
        raise AuthenticationError("Missing refresh token")
    pair, _user = await rotate_refresh(
        session,
        raw_refresh_token=raw,
        settings=settings,
        ip_address=client_ip(request),
        user_agent=request.headers.get("user-agent"),
    )
    _set_refresh_cookie(response, pair.refresh_token, settings, settings.refresh_token_expire_days)
    return TokenOut(access_token=pair.access_token, expires_at=pair.access_expires_at)


@router.post("/logout")
async def logout(
    request: Request,
    response: Response,
    session: DBSession,
    settings: SettingsDep,
    current_user: CurrentUser,
) -> LogoutOut:
    """Revoke the presented refresh token (access tokens expire naturally)."""
    raw = request.cookies.get(REFRESH_COOKIE_NAME)
    if raw:
        await revoke_refresh(session, raw_refresh_token=raw, actor=current_user.email)
    response.delete_cookie(key=REFRESH_COOKIE_NAME, path=_COOKIE_PATH)
    return LogoutOut()


@router.get("/me")
async def me(current_user: CurrentUser) -> UserOut:
    """Return the authenticated caller's profile."""
    return UserOut.model_validate(current_user)
