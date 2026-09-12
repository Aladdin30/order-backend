"""Pydantic schemas for authentication requests, JWT payloads, and user profiles."""

import uuid
from typing import Any

from pydantic import BaseModel, ConfigDict, EmailStr, Field

from app.models.enums import UserRole


class LoginRequest(BaseModel):
    """Credentials payload for direct login."""

    email: EmailStr
    password: str = Field(..., min_length=1)


class TokenResponse(BaseModel):
    """Bearer access token payload returned upon successful authentication."""

    access_token: str
    token_type: str = "bearer"
    expires_in: int


class TokenPayload(BaseModel):
    """Decoded JWT claims representation."""

    sub: str
    tenant_id: str
    role: str
    email: str | None = None
    exp: int | None = None
    iat: int | None = None


class UserResponse(BaseModel):
    """Authenticated user profile with assigned branch authorizations."""

    id: uuid.UUID
    tenant_id: uuid.UUID
    email: str
    full_name: str
    role: UserRole
    is_active: bool
    allowed_branch_ids: list[uuid.UUID] = Field(default_factory=list)

    model_config = ConfigDict(from_attributes=True)
