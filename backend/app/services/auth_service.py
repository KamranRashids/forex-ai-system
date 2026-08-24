"""Authentication business logic.

All token lifecycle rules live here so both the HTTP layer and the CLI share
one implementation:

- Registration: first user becomes ``admin``; later ones default to ``viewer``.
- Login: Argon2 verification + audit trail.
- Rotation: every refresh issues a new token in the same *family*; presenting
  a revoked token is treated as theft and revokes the entire family.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import datetime
from typing import Any, cast

from sqlalchemy import func, select, update
from sqlalchemy.engine import CursorResult
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import Settings, get_settings
from app.core.constants import TOKEN_TYPE_REFRESH, AuditActions
from app.core.errors import AppError, AuthenticationError, ConflictError
from app.core.security import (
    create_access_token,
    create_refresh_token,
    decode_token,
    hash_password,
    password_needs_rehash,
    utcnow,
    verify_password,
)
from app.models.audit_log import AuditLog
from app.models.refresh_token import RefreshToken
from app.models.user import User, UserRole


@dataclass(frozen=True)
class TokenPair:
    access_token: str
    access_expires_at: datetime
    refresh_token: str
    refresh_jti: uuid.UUID
    refresh_family_id: uuid.UUID
    refresh_expires_at: datetime


async def _audit(
    session: AsyncSession,
    *,
    actor: str,
    action: str,
    entity_type: str | None = None,
    entity_id: str | None = None,
    before: dict[str, object] | None = None,
    after: dict[str, object] | None = None,
    ip_address: str | None = None,
) -> None:
    session.add(
        AuditLog(
            actor=actor,
            action=action,
            entity_type=entity_type,
            entity_id=entity_id,
            before=before,
            after=after,
            ip_address=ip_address,
        )
    )


async def _commit_security_event(session: AsyncSession, exc: AppError) -> AppError:
    """Persist audit/security writes, then return the error to raise.

    Request-scoped sessions roll back when a handler raises; security events
    (failed logins, reuse detection) must survive that rollback, so they are
    committed explicitly first.
    """
    await session.commit()
    return exc


async def get_user_by_email(session: AsyncSession, email: str) -> User | None:
    normalized = email.strip().lower()
    result = await session.execute(select(User).where(User.email == normalized))
    return result.scalar_one_or_none()


async def count_users(session: AsyncSession) -> int:
    result = await session.execute(select(func.count()).select_from(User))
    return int(result.scalar_one())


async def register_user(
    session: AsyncSession,
    *,
    email: str,
    password: str,
    ip_address: str | None = None,
    role_override: UserRole | None = None,
) -> User:
    """Create a user; the first account is promoted to admin (bootstrap)."""
    normalized = email.strip().lower()
    if await get_user_by_email(session, normalized) is not None:
        raise ConflictError("Email already registered")

    total_users = await count_users(session)
    role = role_override or (UserRole.ADMIN if total_users == 0 else UserRole.VIEWER)
    user = User(email=normalized, hashed_password=hash_password(password), role=role)
    session.add(user)
    await session.flush()

    await _audit(
        session,
        actor=normalized,
        action=AuditActions.USER_REGISTERED,
        entity_type="user",
        entity_id=str(user.id),
        after={"role": role.value},
        ip_address=ip_address,
    )
    return user


async def authenticate_user(
    session: AsyncSession,
    *,
    email: str,
    password: str,
    ip_address: str | None = None,
) -> User:
    """Verify credentials; raises AuthenticationError on any failure."""
    user = await get_user_by_email(session, email)

    if user is None or not verify_password(user.hashed_password, password):
        await _audit(
            session,
            actor=email.strip().lower(),
            action=AuditActions.LOGIN_FAILED,
            ip_address=ip_address,
        )
        raise await _commit_security_event(
            session, AuthenticationError("Incorrect email or password")
        )

    if not user.is_active:
        await _audit(
            session,
            actor=user.email,
            action=AuditActions.LOGIN_FAILED,
            entity_type="user",
            entity_id=str(user.id),
            after={"reason": "inactive"},
            ip_address=ip_address,
        )
        raise await _commit_security_event(session, AuthenticationError("Account is disabled"))

    if password_needs_rehash(user.hashed_password):
        user.hashed_password = hash_password(password)

    user.last_login_at = utcnow()
    await _audit(
        session,
        actor=user.email,
        action=AuditActions.LOGIN_SUCCEEDED,
        entity_type="user",
        entity_id=str(user.id),
        ip_address=ip_address,
    )
    return user


async def issue_token_pair(
    session: AsyncSession,
    *,
    user: User,
    settings: Settings,
    ip_address: str | None = None,
    user_agent: str | None = None,
    family_id: uuid.UUID | None = None,
) -> TokenPair:
    """Create an access+refresh pair and persist the refresh row."""
    access_token, access_expires_at = create_access_token(
        user_id=user.id, role=user.role.value, settings=settings
    )
    refresh_token, jti, family, refresh_expires_at = create_refresh_token(
        user_id=user.id, family_id=family_id, settings=settings
    )
    session.add(
        RefreshToken(
            user_id=user.id,
            jti=str(jti),
            family_id=family,
            expires_at=refresh_expires_at,
            created_ip=ip_address,
            user_agent=(user_agent or "")[:512] or None,
        )
    )
    return TokenPair(
        access_token=access_token,
        access_expires_at=access_expires_at,
        refresh_token=refresh_token,
        refresh_jti=jti,
        refresh_family_id=family,
        refresh_expires_at=refresh_expires_at,
    )


async def revoke_family(session: AsyncSession, *, family_id: uuid.UUID) -> int:
    """Revoke every active token in a family. Returns rows affected."""
    result = await session.execute(
        update(RefreshToken)
        .where(RefreshToken.family_id == family_id, RefreshToken.revoked_at.is_(None))
        .values(revoked_at=utcnow())
    )
    cursor_result = cast("CursorResult[Any]", result)
    return int(cursor_result.rowcount or 0)


async def get_refresh_row(session: AsyncSession, jti: str) -> RefreshToken | None:
    result = await session.execute(select(RefreshToken).where(RefreshToken.jti == jti))
    return result.scalar_one_or_none()


async def rotate_refresh(
    session: AsyncSession,
    *,
    raw_refresh_token: str,
    settings: Settings,
    ip_address: str | None = None,
    user_agent: str | None = None,
) -> tuple[TokenPair, User]:
    """Rotate a refresh token, enforcing rotation + reuse detection.

    Raises:
        AuthenticationError: invalid/expired token, or reuse detected (the
            whole token family is revoked in the latter case).
    """
    payload = decode_token(raw_refresh_token, expected_type=TOKEN_TYPE_REFRESH, settings=settings)
    jti = str(payload.get("jti", ""))
    row = await get_refresh_row(session, jti)
    if row is None:
        raise AuthenticationError("Unknown refresh token")

    if row.is_revoked:
        # Reuse of an already-rotated/revoked token — treat as credential theft.
        await revoke_family(session, family_id=row.family_id)
        await _audit(
            session,
            actor=str(row.user_id),
            action=AuditActions.TOKEN_REUSE_DETECTED,
            entity_type="refresh_token",
            entity_id=jti,
            after={"family_id": str(row.family_id), "action": "family_revoked"},
            ip_address=ip_address,
        )
        # Commit before raising: the request layer rolls back on errors, and
        # the family revocation must survive regardless of the HTTP outcome.
        raise await _commit_security_event(
            session,
            AuthenticationError("Refresh token reuse detected; session family revoked"),
        )

    user = await session.get(User, row.user_id)
    if user is None or not user.is_active:
        raise AuthenticationError("Account is disabled")

    new_pair = await issue_token_pair(
        session,
        user=user,
        settings=settings,
        ip_address=ip_address,
        user_agent=user_agent,
        family_id=row.family_id,
    )
    row.revoked_at = utcnow()
    row.replaced_by_jti = str(new_pair.refresh_jti)

    await _audit(
        session,
        actor=user.email,
        action=AuditActions.TOKEN_REFRESHED,
        entity_type="user",
        entity_id=str(user.id),
        ip_address=ip_address,
    )
    return new_pair, user


async def revoke_refresh(session: AsyncSession, *, raw_refresh_token: str, actor: str) -> bool:
    """Logout: revoke the presented refresh token. Returns True when revoked now."""
    try:
        payload = decode_token(
            raw_refresh_token, expected_type=TOKEN_TYPE_REFRESH, settings=get_settings()
        )
    except AuthenticationError:
        return False

    row = await get_refresh_row(session, str(payload.get("jti", "")))
    if row is None or row.is_revoked:
        return False

    row.revoked_at = utcnow()
    await _audit(
        session,
        actor=actor,
        action=AuditActions.LOGGED_OUT,
        entity_type="refresh_token",
        entity_id=str(row.jti),
    )
    return True
