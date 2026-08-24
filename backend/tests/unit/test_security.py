"""Unit tests for password hashing, JWT issue/verify, and RBAC ranking."""

from __future__ import annotations

import time
import uuid

import freezegun
import pytest
from app.core.constants import TOKEN_TYPE_ACCESS, TOKEN_TYPE_REFRESH
from app.core.errors import AuthenticationError
from app.core.security import (
    create_access_token,
    create_refresh_token,
    decode_token,
    hash_password,
    password_needs_rehash,
    role_at_least,
    utcnow,
    verify_password,
)


@pytest.mark.unit
def test_password_hash_roundtrip() -> None:
    hashed = hash_password("correct horse battery staple")
    assert hashed != "correct horse battery staple"
    assert verify_password(hashed, "correct horse battery staple")
    assert not verify_password(hashed, "wrong password")


@pytest.mark.unit
def test_fresh_hash_does_not_need_rehash() -> None:
    hashed = hash_password("some-password-123456")
    assert password_needs_rehash(hashed) is False


@pytest.mark.unit
def test_access_token_roundtrip(test_settings) -> None:  # noqa: ANN001 - fixture typing
    user_id = uuid.uuid4()
    token, expires_at = create_access_token(user_id=user_id, role="admin", settings=test_settings)
    payload = decode_token(token, expected_type=TOKEN_TYPE_ACCESS, settings=test_settings)
    assert payload["sub"] == str(user_id)
    assert payload["role"] == "admin"
    assert expires_at > utcnow()


@pytest.mark.unit
def test_refresh_token_carries_family(test_settings) -> None:  # noqa: ANN001
    family = uuid.uuid4()
    token, jti, returned_family, _exp = create_refresh_token(
        user_id=uuid.uuid4(), family_id=family, settings=test_settings
    )
    payload = decode_token(token, expected_type=TOKEN_TYPE_REFRESH, settings=test_settings)
    assert str(jti) == payload["jti"]
    assert str(returned_family) == payload["fam"] == str(family)


@pytest.mark.unit
def test_expired_token_rejected(test_settings) -> None:  # noqa: ANN001 - fixture typing
    user_id = uuid.uuid4()
    with freezegun.freeze_time("2026-01-01T00:00:00Z"):
        token, _exp = create_access_token(user_id=user_id, role="viewer", settings=test_settings)

    # Real clock is far past the frozen issue time -> expired.
    with pytest.raises(AuthenticationError, match="expired"):
        decode_token(token, expected_type=TOKEN_TYPE_ACCESS, settings=test_settings)


@pytest.mark.unit
def test_tampered_signature_rejected(test_settings) -> None:  # noqa: ANN001
    token, _ = create_access_token(user_id=uuid.uuid4(), role="viewer", settings=test_settings)
    tampered = token[:-6] + ("aaaaaa" if not token.endswith("aaaaaa") else "bbbbbb")
    with pytest.raises(AuthenticationError, match="Invalid token"):
        decode_token(tampered, expected_type=TOKEN_TYPE_ACCESS, settings=test_settings)


@pytest.mark.unit
def test_wrong_token_type_rejected(test_settings) -> None:  # noqa: ANN001
    refresh_token, *_ = create_refresh_token(
        user_id=uuid.uuid4(), family_id=None, settings=test_settings
    )
    with pytest.raises(AuthenticationError, match="token type"):
        decode_token(refresh_token, expected_type=TOKEN_TYPE_ACCESS, settings=test_settings)


@pytest.mark.safety
@pytest.mark.unit
@pytest.mark.parametrize(
    ("role", "required", "allowed"),
    [
        ("viewer", "viewer", True),
        ("viewer", "trader", False),
        ("viewer", "admin", False),
        ("trader", "viewer", True),
        ("trader", "admin", False),
        ("admin", "viewer", True),
        ("admin", "trader", True),
        ("admin", "admin", True),
        ("superuser", "admin", False),
        ("admin", "root", False),
    ],
)
def test_role_hierarchy_matrix(role: str, required: str, allowed: bool) -> None:
    assert role_at_least(role, required) is allowed


@pytest.mark.unit
def test_utcnow_is_timezone_aware() -> None:
    now = utcnow()
    assert now.tzinfo is not None
    assert abs(now.timestamp() - time.time()) < 5
