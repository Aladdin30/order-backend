"""Brand administrative router and nested branch onboarding."""

from __future__ import annotations

import uuid
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import RequireRoles, get_async_db
from app.core.context import SecurityContext
from app.models.enums import UserRole
from app.schemas.branch import (
    BranchCreateRequest,
    BranchDetailResponse,
    BranchSummaryResponse,
)
from app.schemas.brand import (
    BrandCreate,
    BrandListResponse,
    BrandResponse,
    BrandUpdate,
)
from app.services.brand_service import BrandService

router = APIRouter(prefix="/brands", tags=["brands"])


def _assert_brand_access(context: SecurityContext, brand_id: uuid.UUID) -> None:
    """Ensure BRAND_ADMIN can only view/mutate their affiliated brand."""
    if context.is_super_admin:
        return
    if context.role == UserRole.BRAND_ADMIN:
        user_brand_id = getattr(context.user, "brand_id", None)
        if user_brand_id is not None and user_brand_id != brand_id:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="ACCESS_FORBIDDEN_BRAND_MISMATCH",
            )
        return
    raise HTTPException(
        status_code=status.HTTP_403_FORBIDDEN,
        detail="ACCESS_FORBIDDEN",
    )


@router.post(
    "",
    response_model=BrandResponse,
    status_code=status.HTTP_201_CREATED,
    summary="Create Brand (Atomic Onboarding)",
    description="Provision a new Brand with optional atomic single-location or multi-branch setup.",
)
async def create_brand(
    payload: BrandCreate,
    db: Annotated[AsyncSession, Depends(get_async_db)],
    context: Annotated[SecurityContext, Depends(RequireRoles([UserRole.SUPER_ADMIN]))],
) -> BrandResponse:
    """Create a new Brand."""
    return await BrandService.create_brand(
        tenant_id=context.tenant_id,
        payload=payload,
        db=db,
        actor_user_id=context.user.id if context.user else None,
        actor_role=context.role.value if context.role else None,
    )


@router.get(
    "",
    response_model=BrandListResponse,
    summary="List Brands",
    description="Retrieve all brands within the organization.",
)
async def list_brands(
    db: Annotated[AsyncSession, Depends(get_async_db)],
    context: Annotated[
        SecurityContext,
        Depends(RequireRoles([UserRole.SUPER_ADMIN, UserRole.BRAND_ADMIN, UserRole.REGIONAL_MANAGER])),
    ],
    is_active: bool | None = Query(default=None, description="Filter by active status"),
    limit: int = Query(default=50, ge=1, le=100),
    offset: int = Query(default=0, ge=0),
) -> BrandListResponse:
    """List organizational brands."""
    if context.role == UserRole.BRAND_ADMIN and getattr(context.user, "brand_id", None) is not None:
        # Scoped brand admin sees only their brand
        brand = await BrandService.get_brand(context.user.brand_id, context.tenant_id, db)
        return BrandListResponse(total=1, items=[brand])

    return await BrandService.list_brands(
        tenant_id=context.tenant_id,
        db=db,
        is_active=is_active,
        limit=limit,
        offset=offset,
    )


@router.get(
    "/{brand_id}",
    response_model=BrandResponse,
    summary="Get Brand Details",
    description="Retrieve full brand metadata including active branches.",
)
async def get_brand(
    brand_id: uuid.UUID,
    db: Annotated[AsyncSession, Depends(get_async_db)],
    context: Annotated[
        SecurityContext,
        Depends(RequireRoles([UserRole.SUPER_ADMIN, UserRole.BRAND_ADMIN, UserRole.REGIONAL_MANAGER])),
    ],
) -> BrandResponse:
    """Retrieve brand by ID."""
    _assert_brand_access(context, brand_id)
    return await BrandService.get_brand(brand_id, context.tenant_id, db)


@router.patch(
    "/{brand_id}",
    response_model=BrandResponse,
    summary="Update Brand",
    description="Modify brand name, slug, or active state.",
)
async def update_brand(
    brand_id: uuid.UUID,
    payload: BrandUpdate,
    db: Annotated[AsyncSession, Depends(get_async_db)],
    context: Annotated[
        SecurityContext,
        Depends(RequireRoles([UserRole.SUPER_ADMIN, UserRole.BRAND_ADMIN])),
    ],
) -> BrandResponse:
    """Update brand metadata."""
    _assert_brand_access(context, brand_id)
    return await BrandService.update_brand(
        brand_id=brand_id,
        tenant_id=context.tenant_id,
        payload=payload,
        db=db,
        actor_user_id=context.user.id if context.user else None,
        actor_role=context.role.value if context.role else None,
    )


@router.delete(
    "/{brand_id}",
    summary="Deactivate Brand",
    description="Soft-deactivate a brand and its affiliated branches.",
)
async def delete_brand(
    brand_id: uuid.UUID,
    db: Annotated[AsyncSession, Depends(get_async_db)],
    context: Annotated[SecurityContext, Depends(RequireRoles([UserRole.SUPER_ADMIN]))],
) -> dict[str, str]:
    """Soft-delete brand."""
    return await BrandService.delete_brand(
        brand_id=brand_id,
        tenant_id=context.tenant_id,
        db=db,
        actor_user_id=context.user.id if context.user else None,
        actor_role=context.role.value if context.role else None,
    )


@router.post(
    "/{brand_id}/branches",
    response_model=BranchDetailResponse,
    status_code=status.HTTP_201_CREATED,
    summary="Create Branch under Brand",
    description="Onboard an operational branch affiliated directly with the brand.",
)
async def create_branch_under_brand(
    brand_id: uuid.UUID,
    payload: BranchCreateRequest,
    db: Annotated[AsyncSession, Depends(get_async_db)],
    context: Annotated[
        SecurityContext,
        Depends(RequireRoles([UserRole.SUPER_ADMIN, UserRole.BRAND_ADMIN])),
    ],
) -> BranchDetailResponse:
    """Create a new branch affiliated with the specified brand."""
    _assert_brand_access(context, brand_id)
    return await BrandService.create_branch_under_brand(
        brand_id=brand_id,
        tenant_id=context.tenant_id,
        payload=payload,
        db=db,
        actor_user_id=context.user.id if context.user else None,
        actor_role=context.role.value if context.role else None,
    )


@router.get(
    "/{brand_id}/branches",
    response_model=list[BranchSummaryResponse],
    summary="List Brand Branches",
    description="Retrieve all operational branches belonging to a brand.",
)
async def list_brand_branches(
    brand_id: uuid.UUID,
    db: Annotated[AsyncSession, Depends(get_async_db)],
    context: Annotated[
        SecurityContext,
        Depends(RequireRoles([UserRole.SUPER_ADMIN, UserRole.BRAND_ADMIN, UserRole.REGIONAL_MANAGER])),
    ],
) -> list[BranchSummaryResponse]:
    """List branches affiliated with the brand."""
    _assert_brand_access(context, brand_id)
    return await BrandService.list_brand_branches(brand_id, context.tenant_id, db)
