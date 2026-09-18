"""POS Cashier and Waiter staff ordering and cancellation endpoints."""

from __future__ import annotations

import uuid
from typing import Annotated

from fastapi import APIRouter, Depends, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import EnforceBranchAccess, RequireRoles, get_async_db
from app.core.context import SecurityContext
from app.models.enums import UserRole
from app.schemas.order import OrderResponse
from app.schemas.pos_order import (
    POSCancelOrderRequest,
    POSCancelOrderResponse,
    POSCheckoutRequest,
)
from app.services.pos_order_service import POSOrderService

router = APIRouter(prefix="/pos", tags=["pos"])


@router.post(
    "/orders/checkout",
    response_model=OrderResponse,
    status_code=status.HTTP_201_CREATED,
    summary="POS Counter & Table-Side Order Placement (Staff)",
    description=(
        "Staff order creation pipeline. Supports dine-in (with table assignment and shared bill re-orders) "
        "and takeaway (with atomic daily pickup sequence generation from 100 to 1000 and immediate payment settlement)."
    ),
)
async def checkout_pos_order(
    payload: POSCheckoutRequest,
    db: Annotated[AsyncSession, Depends(get_async_db)],
    context: Annotated[
        SecurityContext,
        Depends(
            RequireRoles(
                [
                    UserRole.CASHIER,
                    UserRole.BRANCH_ADMIN,
                    UserRole.WAITER,
                    UserRole.SUPER_ADMIN,
                ]
            )
        ),
    ],
    branch_id: Annotated[uuid.UUID, Depends(EnforceBranchAccess())],
) -> OrderResponse:
    """Execute staff-driven POS order creation."""
    return await POSOrderService.checkout_pos_order(
        db=db,
        context=context,
        branch_id=branch_id,
        payload=payload,
    )


@router.post(
    "/orders/{order_id}/cancel",
    response_model=POSCancelOrderResponse,
    summary="Cancel Order from POS with Refund & Table Release (Staff)",
    description=(
        "Cashier cancellation pipeline. Cancels active order, releases occupied physical table, "
        "marks payments as REFUNDED, broadcasts ticket drop event to KDS, and logs audit trail."
    ),
)
async def cancel_pos_order(
    order_id: uuid.UUID,
    payload: POSCancelOrderRequest,
    db: Annotated[AsyncSession, Depends(get_async_db)],
    context: Annotated[
        SecurityContext,
        Depends(
            RequireRoles(
                [
                    UserRole.CASHIER,
                    UserRole.BRANCH_ADMIN,
                    UserRole.SUPER_ADMIN,
                ]
            )
        ),
    ],
    branch_id: Annotated[uuid.UUID, Depends(EnforceBranchAccess())],
) -> POSCancelOrderResponse:
    """Execute cashier order cancellation and refund."""
    return await POSOrderService.cancel_pos_order(
        db=db,
        context=context,
        branch_id=branch_id,
        order_id=order_id,
        payload=payload,
    )
