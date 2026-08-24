"""Admin user management (role changes, deactivation) with audit trail."""

from __future__ import annotations

import uuid
from typing import Annotated

from fastapi import APIRouter, Query, Request
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import AdminUser, DBSession, client_ip
from app.core.constants import AuditActions
from app.core.errors import NotFoundError, PermissionDeniedError
from app.models.audit_log import AuditLog
from app.models.user import User
from app.schemas.user import UserAdminUpdate, UserOut

router = APIRouter(prefix="/users", tags=["users"])


async def _get_user_or_404(session: AsyncSession, user_id: uuid.UUID) -> User:
    user = await session.get(User, user_id)
    if user is None:
        raise NotFoundError("User not found")
    return user


@router.get("")
async def list_users(
    _admin: AdminUser,
    session: DBSession,
    limit: Annotated[int, Query(ge=1, le=200)] = 50,
    offset: Annotated[int, Query(ge=0)] = 0,
) -> list[UserOut]:
    """List user accounts (admin only)."""
    result = await session.execute(
        select(User).order_by(User.created_at).limit(limit).offset(offset)
    )
    return [UserOut.model_validate(u) for u in result.scalars().all()]


@router.patch("/{user_id}")
async def update_user(
    user_id: uuid.UUID,
    body: UserAdminUpdate,
    session: DBSession,
    admin: AdminUser,
    request: Request,
) -> UserOut:
    """Change a user's role or active flag (admin only, audited)."""
    if user_id == admin.id and body.role is not None and body.role != admin.role:
        raise PermissionDeniedError("Admins cannot change their own role")

    user = await _get_user_or_404(session, user_id)
    before = {"role": user.role.value, "is_active": user.is_active}

    if body.role is not None:
        user.role = body.role
    if body.is_active is not None:
        user.is_active = body.is_active

    after = {"role": user.role.value, "is_active": user.is_active}
    session.add(
        AuditLog(
            actor=admin.email,
            action=AuditActions.USER_UPDATED,
            entity_type="user",
            entity_id=str(user.id),
            before=before,
            after=after,
            ip_address=client_ip(request),
        )
    )
    return UserOut.model_validate(user)


@router.get("/count")
async def count_users(_admin: AdminUser, session: DBSession) -> dict[str, int]:
    """Total number of accounts (admin only)."""
    result = await session.execute(select(func.count()).select_from(User))
    return {"count": int(result.scalar_one())}
