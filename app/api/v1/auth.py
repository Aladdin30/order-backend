"""Authentication endpoints: token issuance and user profile."""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Request, status
from fastapi.security import OAuth2PasswordRequestForm
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.api.deps import get_async_db, get_current_user_context
from app.core.config import settings
from app.core.context import SecurityContext
from app.core.rate_limit import rate_limit, resolve_client_ip
from app.core.security import create_access_token, verify_password
from app.models.auth import User
from app.schemas.auth import TokenResponse, UserResponse
from app.services.audit_service import AuditLogger

router = APIRouter(prefix="/auth", tags=["auth"])


@router.post(
    "/token",
    response_model=TokenResponse,
    summary="Obtain OAuth2 Access Token",
    description="Authenticate staff user credentials via OAuth2 password flow and issue signed JWT.",
)
@rate_limit(max_requests=5, window_seconds=60)
async def login_for_access_token(
    request: Request,
    form_data: Annotated[OAuth2PasswordRequestForm, Depends()],
    db: Annotated[AsyncSession, Depends(get_async_db)],
) -> TokenResponse:
    """Issue JWT access token upon valid email/password credentials."""
    client_ip = resolve_client_ip(request)
    user_agent = request.headers.get("user-agent", "unknown")

    # Locate user by email
    stmt = (
        select(User)
        .options(selectinload(User.branch_access))
        .where(User.email == form_data.username)
    )
    result = await db.execute(stmt)
    user = result.scalar_one_or_none()

    # Authenticate credentials with Argon2id
    if not user or not verify_password(form_data.password, user.hashed_password):
        if user:
            # Audit failed attempt with known tenant
            await AuditLogger.log(
                tenant_id=user.tenant_id,
                action="AUTH_LOGIN_FAILED",
                resource_type="USER",
                user_id=user.id,
                actor_role=user.role.value,
                resource_id=str(user.id),
                ip_address=client_ip,
                user_agent=user_agent,
                changes={"email": form_data.username, "reason": "invalid_credentials"},
                status="FAILED",
            )
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Incorrect email or password",
            headers={"WWW-Authenticate": "Bearer"},
        )

    if not user.is_active:
        await AuditLogger.log(
            tenant_id=user.tenant_id,
            action="AUTH_LOGIN_FAILED",
            resource_type="USER",
            user_id=user.id,
            actor_role=user.role.value,
            resource_id=str(user.id),
            ip_address=client_ip,
            user_agent=user_agent,
            changes={"email": form_data.username, "reason": "inactive_account"},
            status="BLOCKED",
        )
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Inactive user account",
        )

    # Encode JWT claims
    token_claims = {
        "sub": str(user.id),
        "tenant_id": str(user.tenant_id),
        "role": user.role.value,
        "email": user.email,
    }
    access_token = create_access_token(token_claims)

    # Audit successful authentication event
    await AuditLogger.log(
        tenant_id=user.tenant_id,
        action="AUTH_LOGIN_SUCCESS",
        resource_type="USER",
        user_id=user.id,
        actor_role=user.role.value,
        resource_id=str(user.id),
        ip_address=client_ip,
        user_agent=user_agent,
        changes={"email": user.email, "role": user.role.value},
        status="SUCCESS",
    )

    return TokenResponse(
        access_token=access_token,
        token_type="bearer",
        expires_in=settings.ACCESS_TOKEN_EXPIRE_MINUTES * 60,
    )


@router.get(
    "/me",
    response_model=UserResponse,
    summary="Get Current User Profile",
    description="Retrieve identity, assigned role, and authorized branches for active security context.",
)
async def read_current_user_profile(
    context: Annotated[SecurityContext, Depends(get_current_user_context)],
) -> UserResponse:
    """Return authenticated actor profile and resolved branch permissions."""
    return UserResponse(
        id=context.user.id,
        tenant_id=context.tenant_id,
        email=context.user.email,
        full_name=context.user.full_name,
        role=context.role,
        is_active=context.user.is_active,
        allowed_branch_ids=list(context.allowed_branch_ids),
    )
