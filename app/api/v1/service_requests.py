"""Quick-service and takeaway request API endpoints for guests and floor staff."""

from __future__ import annotations

import uuid

from fastapi import APIRouter, Depends, Header, Request, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import (
    EnforceBranchAccess,
    RequirePresenceVerified,
    RequireRoles,
    get_async_db,
)
from app.core.context import SecurityContext
from app.core.i18n import parse_accept_language
from app.models.enums import UserRole
from app.schemas.service_request import (
    CreateServiceRequest,
    ServiceRequestResponse,
    UpdateServiceRequestStatus,
)
from app.schemas.session import GuestSessionContext
from app.services.service_request_service import ServiceRequestService

router = APIRouter(prefix="/service-requests", tags=["Service Requests"])


# -------------------------------------------------------------------------
# Guest Endpoints
# -------------------------------------------------------------------------


@router.post(
    "",
    response_model=ServiceRequestResponse,
    status_code=status.HTTP_201_CREATED,
    summary="Submit a table service request",
)
async def create_service_request(
    request: Request,
    payload: CreateServiceRequest,
    session: GuestSessionContext = Depends(RequirePresenceVerified),
    db: AsyncSession = Depends(get_async_db),
    accept_language: str | None = Header(default=None),
) -> ServiceRequestResponse:
    """Verified dining guest submits a categorized table service request."""
    locale = parse_accept_language(accept_language)
    return await ServiceRequestService.create_request(
        db=db,
        session=session,
        payload=payload,
        locale=locale,
    )


@router.get(
    "/table/active",
    response_model=list[ServiceRequestResponse],
    summary="List active service requests for customer's table",
)
async def get_table_active_requests(
    session: GuestSessionContext = Depends(RequirePresenceVerified),
    db: AsyncSession = Depends(get_async_db),
) -> list[ServiceRequestResponse]:
    """Retrieve all pending or acknowledged service requests for the caller's table."""
    return await ServiceRequestService.get_active_requests_for_table(
        db=db,
        table_id=session.table_id,
        table_number=session.table_number,
    )


# -------------------------------------------------------------------------
# Staff Resolution Endpoints
# -------------------------------------------------------------------------


@router.get(
    "/active",
    response_model=list[ServiceRequestResponse],
    summary="Staff FIFO queue of active branch service requests",
)
async def get_active_branch_requests(
    request: Request,
    branch_id: uuid.UUID = Depends(EnforceBranchAccess()),
    context: SecurityContext = Depends(
        RequireRoles([UserRole.WAITER, UserRole.BRANCH_ADMIN])
    ),
    db: AsyncSession = Depends(get_async_db),
) -> list[ServiceRequestResponse]:
    """Retrieve FIFO list of pending and acknowledged service requests for the branch."""
    return await ServiceRequestService.get_active_requests_for_branch(
        db=db,
        branch_id=branch_id,
    )


@router.patch(
    "/{request_id}/status",
    response_model=ServiceRequestResponse,
    summary="Transition service request status",
)
async def update_service_request_status(
    request: Request,
    request_id: uuid.UUID,
    payload: UpdateServiceRequestStatus,
    branch_id: uuid.UUID = Depends(EnforceBranchAccess()),
    context: SecurityContext = Depends(
        RequireRoles([UserRole.WAITER, UserRole.BRANCH_ADMIN])
    ),
    db: AsyncSession = Depends(get_async_db),
    accept_language: str | None = Header(default=None),
) -> ServiceRequestResponse:
    """Floor staff transitions a service request (ACKNOWLEDGED, COMPLETED, DISMISSED)."""
    locale = parse_accept_language(accept_language)
    return await ServiceRequestService.transition_status(
        db=db,
        request_id=request_id,
        branch_id=branch_id,
        tenant_id=context.tenant_id,
        target_status=payload.status,
        actor_id=context.user.id,
        actor_role=context.role.value,
        locale=locale,
    )
