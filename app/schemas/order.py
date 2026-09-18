"""Pydantic v2 schemas for order ingestion, checkout, transitions, and lifecycle responses."""

from __future__ import annotations

import datetime
import uuid
from decimal import Decimal
from typing import Any

from pydantic import AliasChoices, BaseModel, ConfigDict, Field

from app.models.enums import KitchenStation, OrderSource, OrderStatus, OrderType, TableStatus
from app.schemas.menu import SelectedModifierGroupInput


class OrderItemInput(BaseModel):
    """Line item payload submitted during checkout."""

    item_id: uuid.UUID = Field(..., description="Target catalog Item UUID")
    quantity: int = Field(default=1, ge=1, le=100, description="Item quantity (1-100)")
    selected_groups: list[SelectedModifierGroupInput] = Field(
        default_factory=list,
        validation_alias=AliasChoices("selected_groups", "selections", "selected_modifiers", "modifiers"),
        description="Modifier group selections",
    )
    selected_option_ids: list[uuid.UUID] = Field(
        default_factory=list,
        description="UUIDs of chosen modifier options",
    )
    special_instructions: str | None = Field(
        default=None,
        max_length=500,
        description="Guest preparation notes",
    )

    model_config = ConfigDict(populate_by_name=True)


CheckoutItemInput = OrderItemInput


class CheckoutRequest(BaseModel):
    """Atomic order placement payload from verified guest session."""

    items: list[OrderItemInput] = Field(
        ...,
        min_length=1,
        max_length=100,
        description="Array of items to order",
    )
    customer_notes: str | None = Field(
        default=None,
        max_length=500,
        description="General order-level guest notes",
    )


class OrderItemResponse(BaseModel):
    """Immutable snapshot representation of an ordered line item."""

    id: uuid.UUID
    order_id: uuid.UUID | None = None
    item_id: uuid.UUID
    item_name: str | None = None
    quantity: int
    unit_price: Decimal
    subtotal: Decimal
    station: KitchenStation
    station_code: str | None = None
    station_id: uuid.UUID | None = None
    is_bumped: bool
    selected_modifiers: list[dict[str, Any]] = Field(
        default_factory=list,
        description="Immutable snapshot array of modifier choices and price adjustments",
    )
    special_instructions: str | None = None

    model_config = ConfigDict(from_attributes=True)


class OrderResponse(BaseModel):
    """Comprehensive customer order representation with line items and financial totals."""

    id: uuid.UUID
    tenant_id: uuid.UUID
    branch_id: uuid.UUID
    table_id: uuid.UUID | None = None
    status: OrderStatus
    order_type: OrderType
    order_source: OrderSource = OrderSource.QR_CUSTOMER
    pickup_number: int | None = None
    subtotal: Decimal
    service_fee_rate: Decimal = Decimal("0.0000")
    service_fee_total: Decimal = Decimal("0.00")
    applied_tax_rate: Decimal = Decimal("0.0000")
    tax_total: Decimal
    total_amount: Decimal
    is_paid: bool = False
    customer_notes: str | None = None
    cancellation_reason: str | None = None
    items: list[OrderItemResponse] = Field(
        default_factory=list,
        description="Ordered line items with modifier snapshots and station routing",
    )
    created_at: datetime.datetime
    updated_at: datetime.datetime

    model_config = ConfigDict(from_attributes=True)


class OrderTransitionRequest(BaseModel):
    """Staff request to mutate order lifecycle state."""

    target_status: OrderStatus = Field(..., description="Desired target OrderStatus")
    reason: str | None = Field(
        default=None,
        max_length=500,
        description="Mandatory explanation when cancelling an in-progress ticket",
    )


class OrderTransitionResponse(BaseModel):
    """Result of an order lifecycle state mutation."""

    order_id: uuid.UUID
    from_status: OrderStatus
    to_status: OrderStatus
    table_status: TableStatus | None = Field(
        default=None,
        description="Updated physical table status resulting from this transition",
    )
    message: str

    model_config = ConfigDict(from_attributes=True)
