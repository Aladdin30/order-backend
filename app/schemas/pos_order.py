"""Pydantic schemas for POS Cashier and Waiter staff ordering module."""

from __future__ import annotations

import uuid
from decimal import Decimal
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from app.models.enums import OrderType, PaymentMethod
from app.schemas.order import OrderItemInput, OrderResponse


class POSCheckoutRequest(BaseModel):
    """Staff POS payload to place a Dine-In or Takeaway order at counter or table-side."""

    order_type: OrderType = Field(
        default=OrderType.DINE_IN,
        description="Fulfillment channel: DINE_IN requires table_id; TAKEAWAY requires immediate_payment",
    )
    table_id: uuid.UUID | None = Field(
        default=None,
        description="Target physical table UUID. Required when order_type is DINE_IN.",
    )
    items: list[OrderItemInput] = Field(
        ...,
        min_length=1,
        max_length=100,
        description="List of catalog items with customizations",
    )
    immediate_payment: PaymentMethod | None = Field(
        default=None,
        description="Settlement tender type for immediate payment (e.g. CASH, POS_TERMINAL, CARD_TERMINAL). Required for TAKEAWAY.",
    )
    customer_notes: str | None = Field(
        default=None,
        max_length=500,
        description="Customer notes or special requests",
    )

    model_config = ConfigDict(populate_by_name=True)


class POSCancelOrderRequest(BaseModel):
    """Payload to cancel an order and optionally refund collected payments."""

    reason: str = Field(
        ...,
        min_length=3,
        max_length=500,
        description="Mandatory explanation for audit trail and waste tracking",
    )
    refund_payment: bool = Field(
        default=True,
        description="Whether to mark existing settled transactions as REFUNDED",
    )

    model_config = ConfigDict(extra="forbid")


class POSCancelOrderResponse(BaseModel):
    """Result of cashier-initiated cancellation."""

    order_id: uuid.UUID
    status: str
    cancellation_reason: str
    is_refunded: bool
    table_freed: bool
    message: str

    model_config = ConfigDict(from_attributes=True)
