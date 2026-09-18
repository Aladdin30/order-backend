"""Branch administrative management endpoints: geofencing and dynamic financials."""

from __future__ import annotations

import uuid
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Request, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import EnforceBranchAccess, RequireRoles, get_async_db
from app.core.context import SecurityContext
from app.core.rate_limit import resolve_client_ip
from app.models.auth import Branch
from app.models.enums import UserRole
from app.schemas.branch import (
    BranchDetailResponse,
    BranchFinancialSettingsResponse,
    BranchLocationResponse,
    BranchUpdateRequest,
    UpdateBranchFinancialSettingsRequest,
    UpdateBranchLocationRequest,
)
from app.services.audit_service import AuditLogger

router = APIRouter(prefix="/branches", tags=["branches"])


@router.put(
    "/{branch_id}/location",
    response_model=BranchLocationResponse,
    summary="Update Branch GPS Coordinates and Geofence Radius (in meters)",
    description="Update authoritative physical coordinates and geofencing radius in meters for a branch.",
)
async def update_branch_location(
    branch_id: uuid.UUID,
    payload: UpdateBranchLocationRequest,
    request: Request,
    db: Annotated[AsyncSession, Depends(get_async_db)],
    context: Annotated[
        SecurityContext,
        Depends(
            RequireRoles(
                [
                    UserRole.BRANCH_ADMIN,
                    UserRole.REGIONAL_MANAGER,
                    UserRole.SUPER_ADMIN,
                ]
            )
        ),
    ],
    validated_branch_id: Annotated[uuid.UUID, Depends(EnforceBranchAccess())],
) -> BranchLocationResponse:
    """Update branch latitude, longitude, and geofence radius in meters."""
    stmt = select(Branch).where(
        Branch.id == validated_branch_id,
        Branch.tenant_id == context.tenant_id,
    )
    res = await db.execute(stmt)
    branch = res.scalar_one_or_none()

    if not branch:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="BRANCH_NOT_FOUND",
        )

    old_values = {
        "latitude": str(branch.latitude),
        "longitude": str(branch.longitude),
        "geofence_radius_meters": branch.geofence_radius_meters,
    }

    branch.latitude = payload.latitude
    branch.longitude = payload.longitude
    branch.geofence_radius_meters = payload.geofence_radius_meters

    await db.commit()
    await db.refresh(branch)

    # Audit logging
    client_ip = resolve_client_ip(request)
    user_agent = request.headers.get("user-agent", "unknown")
    await AuditLogger.log(
        tenant_id=context.tenant_id,
        action="BRANCH_LOCATION_UPDATED",
        resource_type="BRANCH",
        user_id=context.user.id if context.user else None,
        actor_role=context.role.value,
        resource_id=str(branch.id),
        ip_address=client_ip,
        user_agent=user_agent,
        changes={
            "old": old_values,
            "new": {
                "latitude": str(payload.latitude),
                "longitude": str(payload.longitude),
                "geofence_radius_meters": payload.geofence_radius_meters,
            },
        },
        status="SUCCESS",
    )

    return BranchLocationResponse.model_validate(branch)


@router.put(
    "/{branch_id}/financial-settings",
    response_model=BranchFinancialSettingsResponse,
    summary="Update Branch Dynamic Financial Settings",
    description="Configure branch tax rate, service fee rate, tax-on-service mode, and dine-in exclusivity.",
)
async def update_branch_financial_settings(
    branch_id: uuid.UUID,
    payload: UpdateBranchFinancialSettingsRequest,
    request: Request,
    db: Annotated[AsyncSession, Depends(get_async_db)],
    context: Annotated[
        SecurityContext,
        Depends(
            RequireRoles(
                [
                    UserRole.BRANCH_ADMIN,
                    UserRole.REGIONAL_MANAGER,
                    UserRole.SUPER_ADMIN,
                ]
            )
        ),
    ],
    validated_branch_id: Annotated[uuid.UUID, Depends(EnforceBranchAccess())],
) -> BranchFinancialSettingsResponse:
    """Update dynamic VAT rate and service charge parameters."""
    stmt = select(Branch).where(
        Branch.id == validated_branch_id,
        Branch.tenant_id == context.tenant_id,
    )
    res = await db.execute(stmt)
    branch = res.scalar_one_or_none()

    if not branch:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="BRANCH_NOT_FOUND",
        )

    old_values = {
        "tax_rate": str(branch.tax_rate),
        "service_fee_rate": str(branch.service_fee_rate),
        "is_service_taxable": branch.is_service_taxable,
        "is_tax_inclusive": branch.is_tax_inclusive,
        "service_fee_dine_in_only": branch.service_fee_dine_in_only,
    }

    branch.tax_rate = payload.tax_rate
    branch.service_fee_rate = payload.service_fee_rate
    branch.is_service_taxable = payload.is_service_taxable
    branch.is_tax_inclusive = payload.is_tax_inclusive
    branch.service_fee_dine_in_only = payload.service_fee_dine_in_only

    await db.commit()
    await db.refresh(branch)

    # Audit logging
    client_ip = resolve_client_ip(request)
    user_agent = request.headers.get("user-agent", "unknown")
    await AuditLogger.log(
        tenant_id=context.tenant_id,
        action="BRANCH_FINANCIAL_SETTINGS_UPDATED",
        resource_type="BRANCH",
        user_id=context.user.id if context.user else None,
        actor_role=context.role.value,
        resource_id=str(branch.id),
        ip_address=client_ip,
        user_agent=user_agent,
        changes={
            "old": old_values,
            "new": {
                "tax_rate": str(payload.tax_rate),
                "service_fee_rate": str(payload.service_fee_rate),
                "is_service_taxable": payload.is_service_taxable,
                "is_tax_inclusive": payload.is_tax_inclusive,
                "service_fee_dine_in_only": payload.service_fee_dine_in_only,
            },
        },
        status="SUCCESS",
    )

    return BranchFinancialSettingsResponse(
        branch_id=branch.id,
        tax_rate=branch.tax_rate,
        service_fee_rate=branch.service_fee_rate,
        is_service_taxable=branch.is_service_taxable,
        is_tax_inclusive=branch.is_tax_inclusive,
        service_fee_dine_in_only=branch.service_fee_dine_in_only,
    )


@router.get(
    "/{branch_id}/financial-settings",
    response_model=BranchFinancialSettingsResponse,
    summary="Get Branch Dynamic Financial Settings",
    description="Retrieve branch VAT rate, service fee rate, tax-on-service mode, and dine-in exclusivity.",
)
async def get_branch_financial_settings(
    branch_id: uuid.UUID,
    db: Annotated[AsyncSession, Depends(get_async_db)],
    context: Annotated[
        SecurityContext,
        Depends(
            RequireRoles(
                [
                    UserRole.BRANCH_ADMIN,
                    UserRole.REGIONAL_MANAGER,
                    UserRole.SUPER_ADMIN,
                    UserRole.CASHIER,
                    UserRole.WAITER,
                ]
            )
        ),
    ],
    validated_branch_id: Annotated[uuid.UUID, Depends(EnforceBranchAccess())],
) -> BranchFinancialSettingsResponse:
    """Retrieve current branch financial settings."""
    stmt = select(Branch).where(
        Branch.id == validated_branch_id,
        Branch.tenant_id == context.tenant_id,
    )
    res = await db.execute(stmt)
    branch = res.scalar_one_or_none()

    if not branch:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="BRANCH_NOT_FOUND",
        )

    return BranchFinancialSettingsResponse(
        branch_id=branch.id,
        tax_rate=branch.tax_rate,
        service_fee_rate=branch.service_fee_rate,
        is_service_taxable=branch.is_service_taxable,
        is_tax_inclusive=branch.is_tax_inclusive,
        service_fee_dine_in_only=branch.service_fee_dine_in_only,
    )


@router.get(
    "/{branch_id}",
    response_model=BranchDetailResponse,
    summary="Get Branch Profile",
    description="Retrieve comprehensive operational branch details.",
)
async def get_branch_profile(
    branch_id: uuid.UUID,
    db: Annotated[AsyncSession, Depends(get_async_db)],
    context: Annotated[
        SecurityContext,
        Depends(
            RequireRoles(
                [
                    UserRole.BRANCH_ADMIN,
                    UserRole.REGIONAL_MANAGER,
                    UserRole.BRAND_ADMIN,
                    UserRole.SUPER_ADMIN,
                ]
            )
        ),
    ],
    validated_branch_id: Annotated[uuid.UUID, Depends(EnforceBranchAccess())],
) -> BranchDetailResponse:
    """Retrieve full branch details."""
    stmt = select(Branch).where(
        Branch.id == validated_branch_id,
        Branch.tenant_id == context.tenant_id,
    )
    branch = (await db.execute(stmt)).scalar_one_or_none()
    if not branch:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="BRANCH_NOT_FOUND",
        )
    return BranchDetailResponse.model_validate(branch)


@router.patch(
    "/{branch_id}",
    response_model=BranchDetailResponse,
    summary="Update Branch Profile",
    description="Modify general branch configuration (name, slug, currency, timezone, is_active).",
)
async def update_branch_profile(
    branch_id: uuid.UUID,
    payload: BranchUpdateRequest,
    request: Request,
    db: Annotated[AsyncSession, Depends(get_async_db)],
    context: Annotated[
        SecurityContext,
        Depends(
            RequireRoles(
                [
                    UserRole.BRANCH_ADMIN,
                    UserRole.REGIONAL_MANAGER,
                    UserRole.BRAND_ADMIN,
                    UserRole.SUPER_ADMIN,
                ]
            )
        ),
    ],
    validated_branch_id: Annotated[uuid.UUID, Depends(EnforceBranchAccess())],
) -> BranchDetailResponse:
    """Update general branch settings."""
    stmt = select(Branch).where(
        Branch.id == validated_branch_id,
        Branch.tenant_id == context.tenant_id,
    )
    branch = (await db.execute(stmt)).scalar_one_or_none()
    if not branch:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="BRANCH_NOT_FOUND",
        )

    if payload.slug is not None and payload.slug != branch.slug:
        exists = (
            await db.execute(
                select(Branch.id).where(
                    Branch.tenant_id == context.tenant_id,
                    Branch.slug == payload.slug,
                ).limit(1)
            )
        ).scalar_one_or_none()
        if exists:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail="BRANCH_SLUG_ALREADY_EXISTS",
            )
        branch.slug = payload.slug

    if payload.name is not None:
        branch.name = payload.name
    if payload.brand_id is not None:
        branch.brand_id = payload.brand_id
    if payload.currency is not None:
        branch.currency = payload.currency
    if payload.timezone is not None:
        branch.timezone = payload.timezone
    if payload.is_active is not None:
        branch.is_active = payload.is_active

    await db.commit()
    await db.refresh(branch)

    client_ip = resolve_client_ip(request)
    user_agent = request.headers.get("user-agent", "unknown")
    await AuditLogger.log(
        tenant_id=context.tenant_id,
        branch_id=branch.id,
        action="BRANCH_PROFILE_UPDATED",
        resource_type="BRANCH",
        user_id=context.user.id if context.user else None,
        actor_role=context.role.value,
        resource_id=str(branch.id),
        ip_address=client_ip,
        user_agent=user_agent,
        changes=payload.model_dump(exclude_unset=True),
        status="SUCCESS",
    )

    return BranchDetailResponse.model_validate(branch)

