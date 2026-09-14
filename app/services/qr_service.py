"""QR Service: Business logic and database active-state verification pipeline."""

from __future__ import annotations

import uuid

from fastapi import HTTPException, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.core.qr_security import QRSignatureEngine
from app.models.auth import Branch, Table, Tenant
from app.schemas.qr import QRGenerateTokenResponse, QRVerificationResponse


class QRService:
    """Service managing physical table QR verification and token issuance."""

    @classmethod
    async def verify_table_qr(
        cls,
        db: AsyncSession,
        token: str,
    ) -> QRVerificationResponse:
        """Decode and cryptographically verify table token, resolving active DB state.

        Raises:
            InvalidTokenError / TokenTamperedError / UnsupportedKeyVersionError:
                If token structure, signature, or key version fails.
            HTTPException(403):
                If table, branch, or tenant is inactive or not found.
        """
        # Step 1: Decode and verify signature
        payload = QRSignatureEngine.decode_and_verify_signature(token)

        # Step 2: Fetch Table joined with its Branch and Tenant
        stmt = (
            select(Table)
            .options(selectinload(Table.branch).selectinload(Branch.tenant))
            .where(
                Table.id == payload.table_id,
                Table.branch_id == payload.branch_id,
            )
        )
        result = await db.execute(stmt)
        table = result.scalar_one_or_none()

        # Step 3: Verify entity existence, tenant isolation, and active flags
        if (
            table is None
            or table.branch is None
            or table.branch.tenant is None
            or table.branch.tenant_id != payload.tenant_id
            or not table.is_active
            or not table.branch.is_active
            or not table.branch.tenant.is_active
        ):
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="Branch or table is not currently active",
            )

        # Step 4: Return validated data payload with branch geofence coordinates
        return QRVerificationResponse(
            is_valid=True,
            tenant_id=payload.tenant_id,
            branch_id=payload.branch_id,
            table_id=payload.table_id,
            table_number=table.table_number,
            branch_name=table.branch.name,
            branch_latitude=table.branch.latitude,
            branch_longitude=table.branch.longitude,
            geofence_radius_meters=table.branch.geofence_radius_meters,
            requires_geofence_check=True,
        )

    @classmethod
    async def generate_table_token(
        cls,
        db: AsyncSession,
        tenant_id: uuid.UUID,
        branch_id: uuid.UUID,
        table_id: uuid.UUID,
        key_version: int = 1,
    ) -> QRGenerateTokenResponse:
        """Issue signed QR token after verifying table existence and branch scoping."""
        stmt = (
            select(Table)
            .options(selectinload(Table.branch))
            .where(
                Table.id == table_id,
                Table.branch_id == branch_id,
            )
        )
        result = await db.execute(stmt)
        table = result.scalar_one_or_none()

        if table is None or table.branch is None or table.branch.tenant_id != tenant_id:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=f"Table '{table_id}' not found within authorized branch.",
            )

        token = QRSignatureEngine.sign_table_token(
            tenant_id=tenant_id,
            branch_id=branch_id,
            table_id=table_id,
            key_version=key_version,
        )

        return QRGenerateTokenResponse(
            token=token,
            tenant_id=tenant_id,
            branch_id=branch_id,
            table_id=table_id,
            table_number=table.table_number,
        )
