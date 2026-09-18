"""Pydantic schemas for Kitchen Display System (KDS) sub-tickets and bump bar actions."""

from __future__ import annotations

import datetime
import uuid
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from app.models.enums import KitchenStation, OrderStatus, OrderType


class KDSSubTicketItem(BaseModel):
    """Line item on a kitchen station sub-ticket."""

    order_item_id: uuid.UUID = Field(..., description="Unique OrderItem UUID")
    item_id: uuid.UUID = Field(..., description="Catalog Item UUID")
    name: str = Field(..., description="Item display name")
    quantity: int = Field(..., ge=1, description="Quantity to prepare")
    station: KitchenStation | str = Field(..., description="Operational kitchen station")
    selected_modifiers: list[dict[str, Any]] = Field(
        default_factory=list,
        description="Snapshot of modifier selections",
    )
    special_instructions: str | None = Field(
        default=None,
        description="Kitchen preparation instructions from customer",
    )
    is_bumped: bool = Field(
        default=False,
        description="Whether this item has been marked as prepared on the bump bar",
    )

    model_config = ConfigDict(from_attributes=True)


class KDSSubTicket(BaseModel):
    """Decomposed, station-scoped kitchen sub-ticket."""

    sub_ticket_id: str = Field(..., description="Deterministic sub-ticket identifier")
    order_id: uuid.UUID = Field(..., description="Parent Order UUID")
    branch_id: uuid.UUID = Field(..., description="Branch UUID")
    table_id: uuid.UUID | None = Field(default=None, description="Table UUID (nullable for takeaway)")
    table_number: str | None = Field(default=None, description="Physical table number or identifier")
    pickup_number: int | None = Field(default=None, description="Daily sequential takeaway pickup token number (100-1000)")
    station: KitchenStation | str = Field(..., description="Assigned kitchen station")
    order_status: OrderStatus = Field(..., description="Parent order status")
    order_type: OrderType = Field(default=OrderType.DINE_IN, description="Dine in or takeaway")
    customer_notes: str | None = Field(default=None, description="General order customer notes")
    created_at: datetime.datetime = Field(..., description="Order placement timestamp")
    items: list[KDSSubTicketItem] = Field(default_factory=list, description="Items for this station")
    total_items_count: int = Field(..., ge=0, description="Total items assigned to this sub-ticket")
    bumped_items_count: int = Field(..., ge=0, description="Items marked as bumped")
    is_fully_bumped: bool = Field(..., description="True if all items on this sub-ticket are bumped")

    model_config = ConfigDict(from_attributes=True)


class KDSBumpRequest(BaseModel):
    """Request payload for bumping or unbumping items on the KDS bump bar."""

    is_bumped: bool = Field(default=True, description="True to mark prepared, False to unbump")


class KDSBumpResponse(BaseModel):
    """Result of a KDS bump operation with overall order readiness."""

    order_id: uuid.UUID = Field(..., description="Parent Order UUID")
    order_item_id: uuid.UUID | None = Field(default=None, description="Affected OrderItem UUID if single item")
    station: KitchenStation | str | None = Field(default=None, description="Affected kitchen station")
    is_bumped: bool = Field(..., description="Current bump state of target")
    order_status: OrderStatus = Field(..., description="Current status of the parent order")
    order_fully_prepared: bool = Field(..., description="True if every item in the entire order is bumped")
    bumped_items_count: int = Field(..., ge=0, description="Total bumped items across all stations")
    total_items_count: int = Field(..., ge=0, description="Total items across all stations")

    model_config = ConfigDict(from_attributes=True)
