"""FastAPI security dependencies: authentication, RBAC enforcement, and branch scoping."""

from __future__ import annotations

import uuid
from typing import AsyncGenerator

import jwt
from fastapi import Depends, Header, HTTPException, Request, status
from fastapi.security import OAuth2PasswordBearer
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.core.config import settings
from app.core.context import SecurityContext
from app.core.database import async_session_factory
from app.core.security import decode_access_token
from app.models.auth import Branch, User
from app.models.enums import UserRole

oauth2_scheme = OAuth2PasswordBearer(
    tokenUrl=f"{settings.API_V1_STR}/auth/token",
    auto_error=True,
)


async def get_async_db() -> AsyncGenerator[AsyncSession, None]:
    """Provide an asynchronous database session for route execution."""
    async with async_session_factory() as session:
        yield session


async def get_current_user_context(
    request: Request,
    token: str = Depends(oauth2_scheme),
    db: AsyncSession = Depends(get_async_db),
) -> SecurityContext:
    """Validate bearer JWT, retrieve user with branch access, and construct SecurityContext.

    Raises:
        HTTPException(401): If token is invalid, expired, or user not found/inactive.
    """
    credentials_exception = HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail="Could not validate credentials",
        headers={"WWW-Authenticate": "Bearer"},
    )

    try:
        payload = decode_access_token(token)
        user_id_raw: str | None = payload.get("sub")
        tenant_id_raw: str | None = payload.get("tenant_id")
        if not user_id_raw or not tenant_id_raw:
            raise credentials_exception

        user_id = uuid.UUID(user_id_raw)
        tenant_id = uuid.UUID(tenant_id_raw)
    except (jwt.PyJWTError, ValueError):
        raise credentials_exception

    # Load active user scoped to tenant with eager branch access relations
    stmt = (
        select(User)
        .options(selectinload(User.branch_access))
        .where(
            User.id == user_id,
            User.tenant_id == tenant_id,
            User.is_active.is_(True),
        )
    )
    result = await db.execute(stmt)
    user = result.scalar_one_or_none()

    if user is None:
        raise credentials_exception

    # Resolve allowed branches from explicit M:N associations
    allowed_branch_ids = frozenset(access.branch_id for access in user.branch_access)

    # Populate request.state with verified claims for audit middleware
    request.state.tenant_id = tenant_id
    request.state.user_id = user.id
    request.state.actor_role = user.role.value

    return SecurityContext(
        user=user,
        tenant_id=tenant_id,
        role=user.role,
        allowed_branch_ids=allowed_branch_ids,
    )


class RequireRoles:
    """Dependency factory enforcing role-based permissions."""

    def __init__(self, allowed_roles: list[UserRole] | set[UserRole]):
        self.allowed_roles = set(allowed_roles)

    def __call__(
        self,
        context: SecurityContext = Depends(get_current_user_context),
    ) -> SecurityContext:
        context.assert_roles(self.allowed_roles)
        return context


class EnforceBranchAccess:
    """Dependency factory validating branch tenant isolation and staff branch authorization."""

    def __init__(self, header_name: str = "X-Branch-ID"):
        self.header_name = header_name

    async def __call__(
        self,
        request: Request,
        context: SecurityContext = Depends(get_current_user_context),
        db: AsyncSession = Depends(get_async_db),
    ) -> uuid.UUID:
        # Extract branch ID from all available sources
        # Priority: Path Param > Query Param > Header (most specific wins)
        path_branch = request.path_params.get("branch_id")
        query_branch = request.query_params.get("branch_id")
        header_branch = request.headers.get(self.header_name)

        # Resolve in priority order
        branch_id_str = path_branch or query_branch or header_branch

        if not branch_id_str:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=f"Branch identification required via '{self.header_name}' header or parameter.",
            )

        try:
            branch_id = uuid.UUID(branch_id_str)
        except ValueError:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=f"Malformed branch UUID: '{branch_id_str}'.",
            )

        # Conflict detection: if multiple sources provide different UUIDs, reject
        provided_values: list[str] = [v for v in (path_branch, query_branch, header_branch) if v]
        if len(provided_values) > 1:
            try:
                unique_uuids = {uuid.UUID(v) for v in provided_values}
            except ValueError:
                raise HTTPException(
                    status_code=status.HTTP_400_BAD_REQUEST,
                    detail="One or more branch_id values are malformed UUIDs.",
                )
            if len(unique_uuids) > 1:
                raise HTTPException(
                    status_code=status.HTTP_400_BAD_REQUEST,
                    detail="Conflicting branch_id values across path, query, and header. They must match.",
                )

        # Confirm branch exists and belongs strictly to the actor's tenant
        stmt = select(Branch).where(
            Branch.id == branch_id,
            Branch.tenant_id == context.tenant_id,
            Branch.is_active.is_(True),
        )
        result = await db.execute(stmt)
        branch = result.scalar_one_or_none()

        if not branch:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=f"Branch '{branch_id}' not found within active tenant.",
            )

        # Confirm actor has permission to operate within this branch
        context.assert_branch_access(branch_id)
        request.state.branch_id = branch_id
        return branch_id


# Re-export guest session dependencies
from app.api.deps_session import (  # noqa: E402
    GuestSessionContext,
    RequirePresenceVerified,
    get_current_guest_session,
    require_presence_verified,
)

__all__ = [
    "get_async_db",
    "get_current_user_context",
    "RequireRoles",
    "EnforceBranchAccess",
    "get_current_guest_session",
    "require_presence_verified",
    "RequirePresenceVerified",
    "GuestSessionContext",
]

