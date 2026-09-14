"""QR Service: Business logic and database active-state verification pipeline."""

from __future__ import annotations

import math
import uuid

from fastapi import HTTPException, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import joinedload, selectinload

from app.core.config import settings
from app.core.qr_security import QRSignatureEngine
from app.models.auth import Branch, Table, Tenant
from app.schemas.qr import QRGenerateTokenResponse, QRVerificationResponse


def _haversine_distance_meters(
    lat1: float, lon1: float, lat2: float, lon2: float,
) -> float:
    """Calculate the great-circle distance between two GPS coordinates using the Haversine formula.

    Returns distance in meters.
    """
    R = 6_371_000  # Earth radius in meters

    lat1_r, lat2_r = math.radians(lat1), math.radians(lat2)
    dlat = math.radians(lat2 - lat1)
    dlon = math.radians(lon2 - lon1)

    a = math.sin(dlat / 2) ** 2 + math.cos(lat1_r) * math.cos(lat2_r) * math.sin(dlon / 2) ** 2
    c = 2 * math.atan2(math.sqrt(a), math.sqrt(1 - a))

    return R * c


class QRService:
    """Service managing physical table QR verification and token issuance."""

    @classmethod
    async def verify_table_qr(
        cls,
        db: AsyncSession,
        token: str,
        client_latitude: float | None = None,
        client_longitude: float | None = None,
    ) -> QRVerificationResponse:
        """Decode and cryptographically verify table token, resolving active DB state.

        Raises:
            InvalidTokenError / TokenTamperedError / UnsupportedKeyVersionError:
                If token structure, signature, or key version fails.
            HTTPException(403):
                If table, branch, or tenant is inactive or not found.
                If client coordinates are outside the branch geofence radius.
        """
        # Step 1: Decode and verify signature
        payload = QRSignatureEngine.decode_and_verify_signature(token)

        # Step 2: Fetch Table joined with its Branch and Tenant
        # Using joinedload for Many-to-One relationships (single SQL JOIN, 1 query)
        stmt = (
            select(Table)
            .options(joinedload(Table.branch).joinedload(Branch.tenant))
            .where(
                Table.id == payload.table_id,
                Table.branch_id == payload.branch_id,
            )
        )
        result = await db.execute(stmt)
        table = result.unique().scalar_one_or_none()

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

        # Step 4: Server-side geofence validation
        has_coordinates = client_latitude is not None and client_longitude is not None

        if settings.ENFORCE_GEOFENCE and not has_coordinates:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="GEOLOCATION_REQUIRED",
            )

        if has_coordinates:
            assert client_latitude is not None and client_longitude is not None
            distance_m = _haversine_distance_meters(
                client_latitude,
                client_longitude,
                float(table.branch.latitude),
                float(table.branch.longitude),
            )
            if distance_m > table.branch.geofence_radius_meters:
                raise HTTPException(
                    status_code=status.HTTP_403_FORBIDDEN,
                    detail="OUT_OF_GEOFENCE",
                )

        # Step 5: Return validated data payload with branch geofence coordinates
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
            requires_geofence_check=not has_coordinates,
        )

    @classmethod
    async def generate_table_token(
        cls,
        db: AsyncSession,
        tenant_id: uuid.UUID,
        branch_id: uuid.UUID,
        table_id: uuid.UUID,
        key_version: int | None = None,
    ) -> QRGenerateTokenResponse:
        """Issue signed QR token after verifying table existence, tenant boundary, and active state.

        Performs a single-roundtrip joined query to fetch Table, Branch, and Tenant.
        """
        # Step 1: Query Table joined with Branch and Tenant in a single SQL query
        stmt = (
            select(Table)
            .options(joinedload(Table.branch).joinedload(Branch.tenant))
            .where(
                Table.id == table_id,
                Table.branch_id == branch_id,
            )
        )
        result = await db.execute(stmt)
        table = result.unique().scalar_one_or_none()

        # Step 2: Tenant isolation & existence check (prevents IDOR and cross-tenant leakage)
        if table is None or table.branch is None or table.branch.tenant_id != tenant_id:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=f"Table '{table_id}' not found within authorized branch.",
            )

        # Step 3: Verify Tenant active status
        if not table.branch.tenant.is_active:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="Tenant is not currently active.",
            )

        # Step 4: Verify Table and Branch active status (prevent token generation for inactive resources)
        if not table.is_active or not table.branch.is_active:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="Branch or table is not currently active.",
            )

        # Step 5: Sign cryptographic table token using active (or specified) key version
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

