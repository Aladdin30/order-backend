"""Pydantic v2 schemas for order ingestion, checkout, transitions, and lifecycle responses."""

from __future__ import annotations

import datetime
import uuid
from decimal import Decimal
from typing import Any

from pydantic import AliasChoices, BaseModel, ConfigDict, Field

from app.models.enums import KitchenStation, OrderStatus, OrderType, TableStatus
from app.schemas.menu import SelectedModifierGroupInput


class CheckoutItemInput(BaseModel):
    """Input payload for an individual order line item with modifier customizations."""

    item_id: uuid.UUID = Field(..., description="Catalog item UUID")
    quantity: int = Field(default=1, ge=1, le=100, description="Quantity of this item to order")
    selected_groups: list[SelectedModifierGroupInput] = Field(
        default_factory=list,
        validation_alias=AliasChoices("selected_groups", "selections", "selected_modifiers", "modifiers"),
        description="Modifier group selections",
    )
    special_instructions: str | None = Field(
        default=None,
        max_length=500,
        description="Kitchen preparation notes for this item",
    )

    model_config = ConfigDict(populate_by_name=True)


class CheckoutRequest(BaseModel):
    """Customer checkout payload containing all draft items ready for ordering."""

    items: list[CheckoutItemInput] = Field(
        ...,
        min_length=1,
        description="List of customized items to order",
    )
    customer_notes: str | None = Field(
        default=None,
        max_length=500,
        description="General order instructions for service/kitchen staff",
    )


class OrderItemResponse(BaseModel):
    """Immutable snapshot representation of an ordered line item."""

    id: uuid.UUID
    order_id: uuid.UUID
    item_id: uuid.UUID
    item_name: str = Field(..., description="Localized item name at order time")
    quantity: int
    unit_price: Decimal
    subtotal: Decimal
    station: KitchenStation
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
    table_id: uuid.UUID
    status: OrderStatus
    order_type: OrderType
    subtotal: Decimal
    tax_total: Decimal
    total_amount: Decimal
    customer_notes: str | None = None
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
