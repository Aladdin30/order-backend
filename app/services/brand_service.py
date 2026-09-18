"""Brand and nested branch management service."""

from __future__ import annotations

import logging
import uuid
from typing import Any

from fastapi import HTTPException, status
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.models.auth import Branch
from app.models.brand import Brand
from app.schemas.branch import (
    BranchCreateRequest,
    BranchDetailResponse,
    BranchSummaryResponse,
    BranchUpdateRequest,
)
from app.schemas.brand import (
    BrandCreate,
    BrandListResponse,
    BrandResponse,
    BrandUpdate,
)
from app.services.audit_service import AuditLogger

logger = logging.getLogger(__name__)


class BrandService:
    """Service handling multi-tenant Brand lifecycle and nested Branch onboarding."""

    @staticmethod
    async def create_brand(
        tenant_id: uuid.UUID,
        payload: BrandCreate,
        db: AsyncSession,
        actor_user_id: uuid.UUID | None = None,
        actor_role: str | None = None,
    ) -> BrandResponse:
        """Create a new Brand with optional atomic single-location or multi-branch setup."""
        # 1. Check Brand slug uniqueness
        existing_brand = (
            await db.execute(select(Brand).where(Brand.slug == payload.slug).limit(1))
        ).scalar_one_or_none()
        if existing_brand:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail="BRAND_SLUG_ALREADY_EXISTS",
            )

        brand = Brand(
            name=payload.name,
            slug=payload.slug,
            is_active=payload.is_active,
        )
        db.add(brand)
        await db.flush()

        branches_to_create = []
        if payload.default_branch:
            branches_to_create.append(payload.default_branch)
        if payload.branches:
            branches_to_create.extend(payload.branches)

        created_branches = []
        for b_data in branches_to_create:
            # Check branch slug uniqueness within tenant
            slug_exists = (
                await db.execute(
                    select(Branch.id).where(
                        Branch.tenant_id == tenant_id,
                        Branch.slug == b_data.slug,
                    ).limit(1)
                )
            ).scalar_one_or_none()
            if slug_exists:
                raise HTTPException(
                    status_code=status.HTTP_409_CONFLICT,
                    detail=f"BRANCH_SLUG_ALREADY_EXISTS: {b_data.slug}",
                )

            branch = Branch(
                tenant_id=tenant_id,
                brand_id=brand.id,
                name=b_data.name,
                slug=b_data.slug,
                currency=b_data.currency,
                timezone=b_data.timezone,
                latitude=b_data.latitude,
                longitude=b_data.longitude,
                geofence_radius_meters=b_data.geofence_radius_meters,
                tax_rate=b_data.tax_rate,
                service_fee_rate=b_data.service_fee_rate,
                is_service_taxable=b_data.is_service_taxable,
                is_tax_inclusive=b_data.is_tax_inclusive,
                service_fee_dine_in_only=b_data.service_fee_dine_in_only,
                is_active=b_data.is_active,
            )
            db.add(branch)
            created_branches.append(branch)

        await db.commit()
        await db.refresh(brand)

        # Audit Logging
        await AuditLogger.log(
            tenant_id=tenant_id,
            action="BRAND_CREATED",
            resource_type="BRAND",
            resource_id=str(brand.id),
            user_id=actor_user_id,
            actor_role=actor_role,
            changes={
                "name": brand.name,
                "slug": brand.slug,
                "branches_count": len(created_branches),
            },
            status="SUCCESS",
        )

        return await BrandService.get_brand(brand.id, tenant_id, db)

    @staticmethod
    async def get_brand(
        brand_id: uuid.UUID,
        tenant_id: uuid.UUID,
        db: AsyncSession,
    ) -> BrandResponse:
        """Retrieve brand details along with active and registered branches."""
        stmt = (
            select(Brand)
            .where(Brand.id == brand_id)
            .options(selectinload(Brand.branches))
        )
        brand = (await db.execute(stmt)).scalar_one_or_none()
        if not brand:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="BRAND_NOT_FOUND",
            )

        # Filter branches by tenant_id for tenant safety
        tenant_branches = [b for b in brand.branches if b.tenant_id == tenant_id]

        return BrandResponse(
            id=brand.id,
            name=brand.name,
            slug=brand.slug,
            is_active=brand.is_active,
            branches=[BranchSummaryResponse.model_validate(b) for b in tenant_branches],
            created_at=brand.created_at,
            updated_at=brand.updated_at,
        )

    @staticmethod
    async def list_brands(
        tenant_id: uuid.UUID,
        db: AsyncSession,
        is_active: bool | None = None,
        limit: int = 50,
        offset: int = 0,
    ) -> BrandListResponse:
        """Retrieve list of brands."""
        stmt = select(Brand).options(selectinload(Brand.branches))
        if is_active is not None:
            stmt = stmt.where(Brand.is_active == is_active)

        stmt = stmt.order_by(Brand.created_at.desc()).limit(limit).offset(offset)
        res = await db.execute(stmt)
        brands = res.scalars().all()

        count_stmt = select(func.count(Brand.id))
        if is_active is not None:
            count_stmt = count_stmt.where(Brand.is_active == is_active)
        total = (await db.execute(count_stmt)).scalar() or 0

        items = []
        for brand in brands:
            tenant_branches = [b for b in brand.branches if b.tenant_id == tenant_id]
            items.append(
                BrandResponse(
                    id=brand.id,
                    name=brand.name,
                    slug=brand.slug,
                    is_active=brand.is_active,
                    branches=[BranchSummaryResponse.model_validate(b) for b in tenant_branches],
                    created_at=brand.created_at,
                    updated_at=brand.updated_at,
                )
            )

        return BrandListResponse(total=total, items=items)

    @staticmethod
    async def update_brand(
        brand_id: uuid.UUID,
        tenant_id: uuid.UUID,
        payload: BrandUpdate,
        db: AsyncSession,
        actor_user_id: uuid.UUID | None = None,
        actor_role: str | None = None,
    ) -> BrandResponse:
        """Update brand properties."""
        stmt = select(Brand).where(Brand.id == brand_id).options(selectinload(Brand.branches))
        brand = (await db.execute(stmt)).scalar_one_or_none()
        if not brand:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="BRAND_NOT_FOUND",
            )

        old_values = {
            "name": brand.name,
            "slug": brand.slug,
            "is_active": brand.is_active,
        }

        if payload.slug is not None and payload.slug != brand.slug:
            existing = (
                await db.execute(select(Brand.id).where(Brand.slug == payload.slug).limit(1))
            ).scalar_one_or_none()
            if existing:
                raise HTTPException(
                    status_code=status.HTTP_409_CONFLICT,
                    detail="BRAND_SLUG_ALREADY_EXISTS",
                )
            brand.slug = payload.slug

        if payload.name is not None:
            brand.name = payload.name

        if payload.is_active is not None:
            brand.is_active = payload.is_active

        await db.commit()
        await db.refresh(brand)

        # Audit Logging
        await AuditLogger.log(
            tenant_id=tenant_id,
            action="BRAND_UPDATED",
            resource_type="BRAND",
            resource_id=str(brand.id),
            user_id=actor_user_id,
            actor_role=actor_role,
            changes={"old": old_values, "new": {"name": brand.name, "slug": brand.slug, "is_active": brand.is_active}},
            status="SUCCESS",
        )

        return await BrandService.get_brand(brand.id, tenant_id, db)

    @staticmethod
    async def delete_brand(
        brand_id: uuid.UUID,
        tenant_id: uuid.UUID,
        db: AsyncSession,
        actor_user_id: uuid.UUID | None = None,
        actor_role: str | None = None,
    ) -> dict[str, str]:
        """Soft-deactivate a brand and its affiliated branches."""
        stmt = select(Brand).where(Brand.id == brand_id).options(selectinload(Brand.branches))
        brand = (await db.execute(stmt)).scalar_one_or_none()
        if not brand:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="BRAND_NOT_FOUND",
            )

        brand.is_active = False
        for b in brand.branches:
            if b.tenant_id == tenant_id:
                b.is_active = False

        await db.commit()

        await AuditLogger.log(
            tenant_id=tenant_id,
            action="BRAND_DEACTIVATED",
            resource_type="BRAND",
            resource_id=str(brand.id),
            user_id=actor_user_id,
            actor_role=actor_role,
            changes={"is_active": False},
            status="SUCCESS",
        )

        return {"status": "success", "message": "Brand and affiliated branches deactivated successfully."}

    @staticmethod
    async def create_branch_under_brand(
        brand_id: uuid.UUID,
        tenant_id: uuid.UUID,
        payload: BranchCreateRequest,
        db: AsyncSession,
        actor_user_id: uuid.UUID | None = None,
        actor_role: str | None = None,
    ) -> BranchDetailResponse:
        """Create a new branch affiliated with a brand."""
        brand = (
            await db.execute(select(Brand).where(Brand.id == brand_id).limit(1))
        ).scalar_one_or_none()
        if not brand:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="BRAND_NOT_FOUND",
            )

        # Check slug uniqueness within tenant
        slug_exists = (
            await db.execute(
                select(Branch.id).where(
                    Branch.tenant_id == tenant_id,
                    Branch.slug == payload.slug,
                ).limit(1)
            )
        ).scalar_one_or_none()
        if slug_exists:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail="BRANCH_SLUG_ALREADY_EXISTS",
            )

        branch = Branch(
            tenant_id=tenant_id,
            brand_id=brand.id,
            name=payload.name,
            slug=payload.slug,
            currency=payload.currency,
            timezone=payload.timezone,
            latitude=payload.latitude,
            longitude=payload.longitude,
            geofence_radius_meters=payload.geofence_radius_meters,
            tax_rate=payload.tax_rate,
            service_fee_rate=payload.service_fee_rate,
            is_service_taxable=payload.is_service_taxable,
            is_tax_inclusive=payload.is_tax_inclusive,
            service_fee_dine_in_only=payload.service_fee_dine_in_only,
            is_active=payload.is_active,
        )
        db.add(branch)
        await db.commit()
        await db.refresh(branch)

        await AuditLogger.log(
            tenant_id=tenant_id,
            branch_id=branch.id,
            action="BRANCH_CREATED",
            resource_type="BRANCH",
            resource_id=str(branch.id),
            user_id=actor_user_id,
            actor_role=actor_role,
            changes={"name": branch.name, "slug": branch.slug, "brand_id": str(brand.id)},
            status="SUCCESS",
        )

        return BranchDetailResponse.model_validate(branch)

    @staticmethod
    async def list_brand_branches(
        brand_id: uuid.UUID,
        tenant_id: uuid.UUID,
        db: AsyncSession,
    ) -> list[BranchSummaryResponse]:
        """List all branches belonging to a brand."""
        brand = (
            await db.execute(select(Brand.id).where(Brand.id == brand_id).limit(1))
        ).scalar_one_or_none()
        if not brand:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="BRAND_NOT_FOUND",
            )

        stmt = select(Branch).where(
            Branch.brand_id == brand_id,
            Branch.tenant_id == tenant_id,
        ).order_by(Branch.created_at.asc())
        res = await db.execute(stmt)
        branches = res.scalars().all()
        return [BranchSummaryResponse.model_validate(b) for b in branches]
