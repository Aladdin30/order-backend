"""Session Service: Guest presence verification, table lifecycle management, and session issuance."""

from __future__ import annotations

import uuid

from fastapi import HTTPException, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import joinedload

from app.core.config import settings
from app.core.geo import haversine_distance_meters
from app.core.qr_security import (
    InvalidTokenError,
    QRSignatureEngine,
    RevokedKeyError,
    TokenTamperedError,
    UnsupportedKeyVersionError,
)
from app.core.session_security import create_guest_session_jwt
from app.models.auth import Branch, Table
from app.models.enums import TableStatus
from app.schemas.session import TablePresenceVerifyRequest, TableSessionResponse


class SessionService:
    """Service managing guest presence verification and dining session lifecycle."""

    @classmethod
    async def verify_presence_and_issue_session(
        cls,
        db: AsyncSession,
        request: TablePresenceVerifyRequest,
    ) -> TableSessionResponse:
        """Verify QR token and coordinates, mutate table state, and issue a Guest Session JWT.

        Raises:
            HTTPException(400): If QR token is malformed, invalid, or tampered.
            HTTPException(403, "TABLE_INACTIVE"): If table, branch, or tenant is inactive or missing.
            HTTPException(403, "OUT_OF_GEOFENCE"): If client coordinates are outside the branch geofence boundary.
        """
        # Step 1: Ingest and decode QR token
        try:
            payload = QRSignatureEngine.decode_and_verify_signature(request.qr_token)
        except (InvalidTokenError, TokenTamperedError, UnsupportedKeyVersionError, RevokedKeyError) as exc:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=str(exc),
            ) from exc

        # Step 2: Eagerly fetch Table joined with Branch and Tenant (single SQL JOIN)
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

        # Step 3: Enforce entity existence, tenant isolation, and active state
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
                detail="TABLE_INACTIVE",
            )

        # Step 4: Three-Tier Presence Policy via Haversine calculation
        has_coords = (
            request.client_latitude is not None and request.client_longitude is not None
        )
        computed_distance: float | None = None
        is_presence_verified: bool = False

        if has_coords:
            assert request.client_latitude is not None and request.client_longitude is not None
            computed_distance = haversine_distance_meters(
                request.client_latitude,
                request.client_longitude,
                float(table.branch.latitude),
                float(table.branch.longitude),
            )
            if computed_distance <= table.branch.geofence_radius_meters:
                # Tier 1: Verified Presence
                is_presence_verified = True
            else:
                # Tier 2: Out of Bounds / Remote Abuse
                raise HTTPException(
                    status_code=status.HTTP_403_FORBIDDEN,
                    detail="OUT_OF_GEOFENCE",
                )
        else:
            # Tier 3: Soft Fallback / Permission Denied
            is_presence_verified = False
            computed_distance = None

        # Capture attributes before commit to avoid async lazy-load detachment
        tenant_id = table.branch.tenant_id
        branch_id = table.branch_id
        table_id = table.id
        table_number = table.table_number
        branch_name = table.branch.name

        # Step 5: Table Lifecycle State Transition & Session Identifier Persistence
        if table.status == TableStatus.AVAILABLE:
            session_id = uuid.uuid4()
            table.status = TableStatus.BROWSING
            table.current_session_token = str(session_id)
        elif table.current_session_token is not None:
            # Preserve existing table session token for co-diners at an active table
            try:
                session_id = uuid.UUID(table.current_session_token)
            except ValueError:
                session_id = uuid.uuid4()
                table.current_session_token = str(session_id)
        else:
            session_id = uuid.uuid4()
            table.current_session_token = str(session_id)

        current_status = table.status

        await db.commit()

        # Step 6: Issue Guest Session JWT
        session_token = create_guest_session_jwt(
            session_id=session_id,
            tenant_id=tenant_id,
            branch_id=branch_id,
            table_id=table_id,
            table_number=table_number,
            is_presence_verified=is_presence_verified,
        )

        return TableSessionResponse(
            session_id=session_id,
            session_token=session_token,
            token_type="bearer",
            tenant_id=tenant_id,
            branch_id=branch_id,
            branch_name=branch_name,
            table_id=table_id,
            table_number=table_number,
            table_status=current_status,
            is_presence_verified=is_presence_verified,
            computed_distance_meters=computed_distance,
            expires_in=settings.GUEST_SESSION_EXPIRE_MINUTES * 60,
        )
