"""Security primitives: password hashing, JWT issuance/verification, RBAC.

- Passwords are hashed with Argon2id (argon2-cffi).
- Access tokens are short-lived JWTs (HS256, ``SECRET_KEY``); refresh tokens
  are longer-lived JWTs whose ``jti`` is persisted for rotation and
  reuse-detection (see ``app/services/auth_service.py``).
- Role checks use a strict hierarchy: viewer < trader < admin.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta
from typing import Any

import jwt
from argon2 import PasswordHasher
from argon2.exceptions import InvalidHashError, VerificationError, VerifyMismatchError

from app.core.config import Settings
from app.core.constants import (
    JWT_ALGORITHM,
    TOKEN_TYPE_ACCESS,
    TOKEN_TYPE_REFRESH,
)
from app.core.errors import AuthenticationError

_password_hasher: PasswordHasher = PasswordHasher()

#: Role privilege ranking (higher wins).
ROLE_RANK: dict[str, int] = {"viewer": 0, "trader": 1, "admin": 2}

VALID_ROLES: frozenset[str] = frozenset(ROLE_RANK)


def utcnow() -> datetime:
    """Timezone-aware UTC now — the only clock the application uses."""
    return datetime.now(UTC)


def hash_password(password: str) -> str:
    return _password_hasher.hash(password)


def verify_password(password_hash: str, password: str) -> bool:
    try:
        return _password_hasher.verify(password_hash, password)
    except (VerifyMismatchError, VerificationError, InvalidHashError):
        return False


def password_needs_rehash(password_hash: str) -> bool:
    return _password_hasher.check_needs_rehash(password_hash)


def role_at_least(role: str, required: str) -> bool:
    """True when ``role`` has at least the privileges of ``required``."""
    if role not in ROLE_RANK or required not in ROLE_RANK:
        return False
    return ROLE_RANK[role] >= ROLE_RANK[required]


def create_access_token(
    *, user_id: uuid.UUID, role: str, settings: Settings
) -> tuple[str, datetime]:
    """Return (encoded_jwt, expires_at) for a short-lived access token."""
    now = utcnow()
    expires_at = now + timedelta(minutes=settings.access_token_expire_minutes)
    payload: dict[str, Any] = {
        "sub": str(user_id),
        "role": role,
        "type": TOKEN_TYPE_ACCESS,
        "jti": uuid.uuid4().hex,
        "iat": int(now.timestamp()),
        "exp": int(expires_at.timestamp()),
    }
    token = jwt.encode(payload, settings.secret_key, algorithm=JWT_ALGORITHM)
    return token, expires_at


def create_refresh_token(
    *, user_id: uuid.UUID, family_id: uuid.UUID | None, settings: Settings
) -> tuple[str, uuid.UUID, uuid.UUID, datetime]:
    """Return (encoded_jwt, jti, family_id, expires_at) for a refresh token.

    ``family_id`` links a rotation chain; reuse detection revokes the whole
    family. A new login starts a fresh family.
    """
    family = family_id or uuid.uuid4()
    jti = uuid.uuid4()
    now = utcnow()
    expires_at = now + timedelta(days=settings.refresh_token_expire_days)
    payload: dict[str, Any] = {
        "sub": str(user_id),
        "type": TOKEN_TYPE_REFRESH,
        "jti": str(jti),
        "fam": str(family),
        "iat": int(now.timestamp()),
        "exp": int(expires_at.timestamp()),
    }
    token = jwt.encode(payload, settings.secret_key, algorithm=JWT_ALGORITHM)
    return token, jti, family, expires_at


def decode_token(token: str, *, expected_type: str, settings: Settings) -> dict[str, Any]:
    """Decode and validate a JWT of the expected type.

    Raises:
        AuthenticationError: on signature/expiry/type/format problems.
    """
    try:
        payload: dict[str, Any] = jwt.decode(token, settings.secret_key, algorithms=[JWT_ALGORITHM])
    except jwt.ExpiredSignatureError as exc:
        raise AuthenticationError("Token has expired") from exc
    except jwt.InvalidTokenError as exc:
        raise AuthenticationError("Invalid token") from exc

    if payload.get("type") != expected_type:
        raise AuthenticationError("Invalid token type")
    if not payload.get("sub"):
        raise AuthenticationError("Invalid token subject")
    return payload
