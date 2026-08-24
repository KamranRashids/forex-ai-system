"""User response and admin-update models."""

from __future__ import annotations

from datetime import datetime
from uuid import UUID

from pydantic import BaseModel, ConfigDict, EmailStr

from app.models.user import UserRole


class UserOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    email: EmailStr
    role: UserRole
    is_active: bool
    last_login_at: datetime | None
    created_at: datetime


class UserAdminUpdate(BaseModel):
    """Fields an admin may change on another user. Omitted fields are untouched."""

    role: UserRole | None = None
    is_active: bool | None = None
