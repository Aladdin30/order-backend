"""Order placement, live tracker, and FSM transition API endpoints."""

from __future__ import annotations

import uuid
from typing import Annotated

from fastapi import APIRouter, Body, Depends, HTTPException, Request, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import (
    EnforceBranchAccess,
    RequireRoles,
    get_async_db,
    get_current_guest_session,
)
from app.core.context import SecurityContext
from app.models.enums import OrderStatus, UserRole
from app.schemas.order import (
    CheckoutRequest,
    OrderResponse,
    OrderTransitionRequest,
    OrderTransitionResponse,
)
from app.schemas.session import GuestSessionContext
from app.services.order_service import OrderService

router = APIRouter(prefix="/orders", tags=["orders"])


@router.post(
    "/checkout",
    response_model=OrderResponse,
    status_code=status.HTTP_201_CREATED,
    summary="Checkout Customer Order with ACID Pessimistic Concurrency",
    description=(
        "Atomic order ingestion pipeline. Acquires a pessimistic row-level lock (SELECT ... FOR UPDATE) "
        "on the Table record, authoritatively re-validates all submitted items and modifier options, "
        "appends to existing active orders for live shared table bills (or initializes new Order), "
        "computes exact 15% VAT totals using Decimal ROUND_HALF_UP, sets presence-driven initial status, "
        "and logs an audit entry."
    ),
)
async def checkout_order(
    body: CheckoutRequest,
    db: Annotated[AsyncSession, Depends(get_async_db)],
    session: Annotated[GuestSessionContext, Depends(get_current_guest_session)],
) -> OrderResponse:
    """Execute ACID checkout pipeline bound to verified guest table session."""
    return await OrderService.checkout_order(
        db=db,
        session=session,
        payload=body,
    )


@router.get(
    "/active",
    response_model=OrderResponse,
    summary="Get Active Table Order for Real-Time Live Tracking",
    description=(
        "Retrieves the active, open order and item fulfillment progress for the table bound "
        "to the verified guest session."
    ),
)
async def get_active_order(
    db: Annotated[AsyncSession, Depends(get_async_db)],
    session: Annotated[GuestSessionContext, Depends(get_current_guest_session)],
) -> OrderResponse:
    """Retrieve the current active order for the guest's dining table."""
    active_order = await OrderService.get_active_table_order(
        db=db,
        branch_id=session.branch_id,
        table_id=session.table_id,
    )
    if active_order is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="NO_ACTIVE_ORDER",
        )
    return active_order


@router.post(
    "/{order_id}/transition",
    response_model=OrderTransitionResponse,
    summary="Transition Order Lifecycle Status (Staff)",
    description=(
        "Authoritative staff endpoint governing FSM state mutations. Enforces valid transition paths, "
        "cancellation guard rules, synchronizes table operational statuses, and writes to the audit trail."
    ),
)
async def transition_order_status(
    order_id: uuid.UUID,
    body: OrderTransitionRequest,
    db: Annotated[AsyncSession, Depends(get_async_db)],
    context: Annotated[
        SecurityContext,
        Depends(
            RequireRoles(
                [
                    UserRole.BRANCH_ADMIN,
                    UserRole.KITCHEN_STAFF,
                    UserRole.CASHIER,
                    UserRole.REGIONAL_MANAGER,
                    UserRole.SUPER_ADMIN,
                ]
            )
        ),
    ],
    branch_id: Annotated[uuid.UUID, Depends(EnforceBranchAccess())],
) -> OrderTransitionResponse:
    """Execute staff-driven order FSM status mutation."""
    actor_id = context.user.id if context.user else None
    actor_role = context.role

    return await OrderService.transition_order_status(
        db=db,
        order_id=order_id,
        target_status=body.target_status,
        reason=body.reason,
        actor_role=actor_role,
        actor_id=actor_id,
        tenant_id=context.tenant_id,
        branch_id=branch_id,
    )


@router.post(
    "/{order_id}/cancel",
    response_model=OrderTransitionResponse,
    summary="Cancel Order (Customer or Staff)",
    description=(
        "Permits customers to cancel orders in DRAFT or PENDING_STAFF_CONFIRMATION state. "
        "Rejects customer cancellation once an order is SUBMITTED or in preparation."
    ),
)
async def cancel_order(
    order_id: uuid.UUID,
    db: Annotated[AsyncSession, Depends(get_async_db)],
    session: Annotated[GuestSessionContext, Depends(get_current_guest_session)],
    reason: str | None = Body(None, embed=True),
) -> OrderTransitionResponse:
    """Customer-initiated cancellation of draft or unconfirmed orders."""
    return await OrderService.transition_order_status(
        db=db,
        order_id=order_id,
        target_status=OrderStatus.CANCELLED,
        reason=reason or "Customer cancelled",
        actor_role="GUEST",
        tenant_id=session.tenant_id,
        branch_id=session.branch_id,
    )
