"""Kitchen Display System (KDS) REST API endpoints for operational station screens and bump bars."""

from __future__ import annotations

import uuid
from typing import Annotated

from fastapi import APIRouter, Body, Depends, Query, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import EnforceBranchAccess, RequireRoles, get_async_db
from app.core.context import SecurityContext
from app.models.enums import KitchenStation, UserRole
from app.schemas.kds import (
    KDSBumpRequest,
    KDSBumpResponse,
    KDSSubTicket,
)
from app.services.station_routing_service import StationRoutingService

router = APIRouter(prefix="/kds", tags=["kds"])

# RBAC Permissions: Kitchen Display Systems are accessible by Kitchen Staff, Branch Admins, and Super Admins
_kds_guard = RequireRoles([
    UserRole.KITCHEN_STAFF,
    UserRole.BRANCH_ADMIN,
    UserRole.SUPER_ADMIN,
])
_branch_guard = EnforceBranchAccess()


@router.get(
    "/tickets",
    response_model=list[KDSSubTicket],
    summary="List Active KDS Sub-Tickets",
    description="Retrieve all active kitchen sub-tickets (SUBMITTED, PREPARING) for a branch in FIFO order, optionally filtered by kitchen station.",
)
async def list_active_tickets(
    branch_id: Annotated[uuid.UUID, Depends(_branch_guard)],
    context: Annotated[SecurityContext, Depends(_kds_guard)],
    db: Annotated[AsyncSession, Depends(get_async_db)],
    station: KitchenStation | None = Query(None, description="Filter by operational station"),
) -> list[KDSSubTicket]:
    """Return active sub-tickets for the specified branch and optional station."""
    return await StationRoutingService.get_active_kds_tickets(
        db=db,
        branch_id=branch_id,
        station=station,
    )


@router.post(
    "/items/{order_item_id}/bump",
    response_model=KDSBumpResponse,
    summary="Bump / Unbump Individual Item Line",
    description="Toggle or mark a single item line as prepared on the bump bar. Automatically synchronizes parent order status to PREPARING or READY.",
)
async def bump_order_item(
    order_item_id: uuid.UUID,
    branch_id: Annotated[uuid.UUID, Depends(_branch_guard)],
    context: Annotated[SecurityContext, Depends(_kds_guard)],
    db: Annotated[AsyncSession, Depends(get_async_db)],
    payload: KDSBumpRequest = Body(default_factory=KDSBumpRequest),
) -> KDSBumpResponse:
    """Bump individual item line with pessimistic concurrency locking."""
    return await StationRoutingService.bump_order_item(
        db=db,
        order_item_id=order_item_id,
        is_bumped=payload.is_bumped,
        actor_role=context.role.value,
        branch_id=branch_id,
    )


@router.post(
    "/orders/{order_id}/stations/{station}/bump",
    response_model=KDSBumpResponse,
    summary="Bulk Bump All Items for a Station",
    description="Mark all items for a given operational kitchen station on an order as bumped in a single action.",
)
async def bump_station_ticket(
    order_id: uuid.UUID,
    station: KitchenStation,
    branch_id: Annotated[uuid.UUID, Depends(_branch_guard)],
    context: Annotated[SecurityContext, Depends(_kds_guard)],
    db: Annotated[AsyncSession, Depends(get_async_db)],
    payload: KDSBumpRequest = Body(default_factory=KDSBumpRequest),
) -> KDSBumpResponse:
    """Bulk bump all items belonging to a station on an order."""
    return await StationRoutingService.bump_station_ticket(
        db=db,
        order_id=order_id,
        station=station,
        is_bumped=payload.is_bumped,
        actor_role=context.role.value,
        branch_id=branch_id,
    )
