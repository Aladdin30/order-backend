"""Service request lifecycle, anti-spam deduplication, and staff dispatch engine."""

from __future__ import annotations

import asyncio
import datetime
import logging
import uuid
from typing import Sequence

from fastapi import HTTPException, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.core.i18n import SupportedLocale, get_localized_message
from app.models.auth import Table
from app.models.enums import ServiceRequestStatus, ServiceRequestType
from app.models.service import ServiceRequest
from app.schemas.service_request import (
    CreateServiceRequest,
    ServiceRequestResponse,
)
from app.schemas.session import GuestSessionContext
from app.services.audit_service import AuditLogger

logger = logging.getLogger(__name__)

# Valid FSM transitions for table-side service requests
LEGAL_SERVICE_REQUEST_TRANSITIONS: dict[ServiceRequestStatus, set[ServiceRequestStatus]] = {
    ServiceRequestStatus.PENDING: {
        ServiceRequestStatus.ACKNOWLEDGED,
        ServiceRequestStatus.COMPLETED,
        ServiceRequestStatus.DISMISSED,
    },
    ServiceRequestStatus.ACKNOWLEDGED: {
        ServiceRequestStatus.COMPLETED,
        ServiceRequestStatus.DISMISSED,
    },
    ServiceRequestStatus.COMPLETED: set(),
    ServiceRequestStatus.DISMISSED: set(),
}

TABLE_REQUEST_COOLDOWN_SECONDS: int = 60


def _ensure_utc(dt: datetime.datetime) -> datetime.datetime:
    """Ensure a datetime object is timezone-aware UTC."""
    if dt.tzinfo is None:
        return dt.replace(tzinfo=datetime.timezone.utc)
    return dt.astimezone(datetime.timezone.utc)


def build_service_request_response(
    req: ServiceRequest,
    table_number: str | None = None,
) -> ServiceRequestResponse:
    """Safely map ServiceRequest ORM model to response schema."""
    resolved_table_number = table_number
    if resolved_table_number is None and "table" in req.__dict__ and req.table:
        resolved_table_number = req.table.table_number

    return ServiceRequestResponse(
        id=req.id,
        branch_id=req.branch_id,
        table_id=req.table_id,
        table_number=resolved_table_number,
        request_type=req.request_type,
        status=req.status,
        note=req.note,
        escalated=req.escalated,
        created_at=req.created_at,
        acknowledged_at=req.acknowledged_at,
        completed_at=req.completed_at,
        dismissed_at=req.dismissed_at,
    )


class ServiceRequestService:
    """Service request ingestion, deduplication, cooldown, and dispatch service."""

    _table_locks: dict[uuid.UUID, asyncio.Lock] = {}

    @classmethod
    def _get_table_lock(cls, table_id: uuid.UUID) -> asyncio.Lock:
        if table_id not in cls._table_locks:
            cls._table_locks[table_id] = asyncio.Lock()
        return cls._table_locks[table_id]

    @classmethod
    async def create_request(
        cls,
        db: AsyncSession,
        session: GuestSessionContext,
        payload: CreateServiceRequest,
        locale: SupportedLocale | None = None,
    ) -> ServiceRequestResponse:
        """Create a new service request with anti-spam duplicate guard and 60-second cooldown."""
        async with cls._get_table_lock(session.table_id):
            now = datetime.datetime.now(datetime.timezone.utc)

            # 0. Pessimistic Row Lock on Table: Serialize concurrent service requests for this table
            stmt_table = (
                select(Table)
                .where(Table.id == session.table_id)
                .with_for_update()
            )
            table = (await db.execute(stmt_table)).scalar_one_or_none()
            if table is None or not table.is_active or table.branch_id != session.branch_id:
                raise HTTPException(
                    status_code=status.HTTP_403_FORBIDDEN,
                    detail=get_localized_message("TABLE_INACTIVE", locale),
                )

            # 1. Active Duplicate Guard: Table cannot open a second request of the same type if active
            stmt_active = (
                select(ServiceRequest)
                .where(
                    ServiceRequest.table_id == session.table_id,
                    ServiceRequest.request_type == payload.request_type,
                    ServiceRequest.status.in_([
                        ServiceRequestStatus.PENDING,
                        ServiceRequestStatus.ACKNOWLEDGED,
                    ]),
                )
                .limit(1)
            )
            active_req = (await db.execute(stmt_active)).scalar_one_or_none()
            if active_req is not None:
                raise HTTPException(
                    status_code=status.HTTP_409_CONFLICT,
                    detail=get_localized_message("ACTIVE_REQUEST_EXISTS", locale),
                )

            # 2. Table Cooldown: 60-second cooldown between consecutive requests of the same type
            stmt_latest = (
                select(ServiceRequest)
                .where(
                    ServiceRequest.table_id == session.table_id,
                    ServiceRequest.request_type == payload.request_type,
                )
                .order_by(ServiceRequest.created_at.desc())
                .limit(1)
            )
            latest_req = (await db.execute(stmt_latest)).scalar_one_or_none()
            if latest_req is not None and latest_req.created_at is not None:
                latest_created_at = _ensure_utc(latest_req.created_at)
                elapsed_seconds = (now - latest_created_at).total_seconds()
                if elapsed_seconds < 60:
                    raise HTTPException(
                        status_code=status.HTTP_429_TOO_MANY_REQUESTS,
                        detail=get_localized_message("SERVICE_REQUEST_COOLDOWN", locale),
                    )

            # 3. Create new service request
            new_request = ServiceRequest(
                branch_id=session.branch_id,
                table_id=session.table_id,
                request_type=payload.request_type,
                status=ServiceRequestStatus.PENDING,
                note=payload.note,
                escalated=False,
                created_at=now,
                updated_at=now,
            )
            db.add(new_request)
            await db.flush()

            response = build_service_request_response(new_request, table_number=session.table_number)

            await db.commit()

        # 4. Audit Log
        await AuditLogger.log(
            tenant_id=session.tenant_id,
            action="SERVICE_REQUEST_CREATED",
            resource_type="service_request",
            resource_id=str(new_request.id),
            branch_id=session.branch_id,
            actor_role="GUEST",
            changes={
                "request_type": new_request.request_type.value,
                "table_id": str(session.table_id),
                "table_number": session.table_number,
                "note": payload.note,
            },
        )

        return response

    @classmethod
    async def get_active_requests_for_branch(
        cls,
        db: AsyncSession,
        branch_id: uuid.UUID,
    ) -> list[ServiceRequestResponse]:
        """Fetch FIFO queue of pending and acknowledged service requests for staff floor view."""
        stmt = (
            select(ServiceRequest)
            .options(selectinload(ServiceRequest.table))
            .where(
                ServiceRequest.branch_id == branch_id,
                ServiceRequest.status.in_([
                    ServiceRequestStatus.PENDING,
                    ServiceRequestStatus.ACKNOWLEDGED,
                ]),
            )
            .order_by(ServiceRequest.created_at.asc())
        )
        result = await db.execute(stmt)
        requests = result.scalars().all()
        return [build_service_request_response(req) for req in requests]

    @classmethod
    async def get_active_requests_for_table(
        cls,
        db: AsyncSession,
        table_id: uuid.UUID,
        table_number: str | None = None,
    ) -> list[ServiceRequestResponse]:
        """Fetch active service requests currently pending or acknowledged for a table."""
        stmt = (
            select(ServiceRequest)
            .options(selectinload(ServiceRequest.table))
            .where(
                ServiceRequest.table_id == table_id,
                ServiceRequest.status.in_([
                    ServiceRequestStatus.PENDING,
                    ServiceRequestStatus.ACKNOWLEDGED,
                ]),
            )
            .order_by(ServiceRequest.created_at.asc())
        )
        result = await db.execute(stmt)
        requests = result.scalars().all()
        return [build_service_request_response(req, table_number=table_number) for req in requests]

    @classmethod
    async def transition_status(
        cls,
        db: AsyncSession,
        request_id: uuid.UUID,
        branch_id: uuid.UUID,
        tenant_id: uuid.UUID,
        target_status: ServiceRequestStatus,
        actor_id: uuid.UUID,
        actor_role: str,
        locale: SupportedLocale | None = None,
    ) -> ServiceRequestResponse:
        """Advance the service request lifecycle status and update relevant timestamps."""
        now = datetime.datetime.now(datetime.timezone.utc)

        stmt = (
            select(ServiceRequest)
            .options(selectinload(ServiceRequest.table))
            .where(
                ServiceRequest.id == request_id,
                ServiceRequest.branch_id == branch_id,
            )
        )
        result = await db.execute(stmt)
        req = result.scalar_one_or_none()

        if req is None:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=get_localized_message("SERVICE_REQUEST_NOT_FOUND", locale),
            )

        # Validate legal transition
        allowed_targets = LEGAL_SERVICE_REQUEST_TRANSITIONS.get(req.status, set())
        if target_status not in allowed_targets:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail=get_localized_message("INVALID_SERVICE_REQUEST_TRANSITION", locale),
            )

        old_status = req.status
        req.status = target_status
        req.updated_at = now

        if target_status == ServiceRequestStatus.ACKNOWLEDGED:
            req.acknowledged_at = now
        elif target_status == ServiceRequestStatus.COMPLETED:
            req.completed_at = now
            if req.acknowledged_at is None:
                req.acknowledged_at = now
        elif target_status == ServiceRequestStatus.DISMISSED:
            req.dismissed_at = now

        table_number = req.table.table_number if req.table else None
        response = build_service_request_response(req, table_number=table_number)

        await db.commit()

        # Audit Log
        await AuditLogger.log(
            tenant_id=tenant_id,
            action="SERVICE_REQUEST_UPDATED",
            resource_type="service_request",
            resource_id=str(req.id),
            branch_id=branch_id,
            user_id=actor_id,
            actor_role=actor_role,
            changes={
                "status": {
                    "old": old_status.value,
                    "new": target_status.value,
                },
                "acknowledged_at": req.acknowledged_at.isoformat() if req.acknowledged_at else None,
                "completed_at": req.completed_at.isoformat() if req.completed_at else None,
                "dismissed_at": req.dismissed_at.isoformat() if req.dismissed_at else None,
            },
        )

        return response
