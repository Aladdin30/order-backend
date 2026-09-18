"""Floor Schemas: Real-time physical table lifecycle states and live occupancy models."""

from __future__ import annotations

import uuid
from decimal import Decimal
from enum import StrEnum

from pydantic import BaseModel, ConfigDict

from app.models.enums import OrderStatus


class TableOccupancyState(StrEnum):
    """Dynamic operational occupancy states for floor tables."""

    AVAILABLE = "AVAILABLE"          # Table is empty; no active sessions or unclosed orders
    SEATED = "SEATED"                # Table session active / QR scanned, but no submitted order
    AWAITING_FOOD = "AWAITING_FOOD"  # Active order in SUBMITTED or PREPARING
    FOOD_SERVED = "FOOD_SERVED"      # Active order in READY, SERVED, or DELIVERED
    BILL_REQUESTED = "BILL_REQUESTED"# Bill settlement requested / checkout pending


class FloorTableLiveResponse(BaseModel):
    """Live snapshot of an individual dining table on the restaurant floor."""

    model_config = ConfigDict(from_attributes=True)

    table_id: uuid.UUID
    table_number: str
    capacity: int
    current_state: TableOccupancyState
    occupancy_duration_minutes: int = 0
    active_session_id: uuid.UUID | None = None
    active_order_id: uuid.UUID | None = None
    order_status: OrderStatus | None = None
    order_total: Decimal | None = None
    pending_service_requests_count: int = 0


class FloorSummaryResponse(BaseModel):
    """Aggregated floor map summary across all dining tables in a branch."""

    total_tables: int
    available_tables: int
    occupied_tables: int
    tables_awaiting_food: int
    tables_with_pending_requests: int
    tables: list[FloorTableLiveResponse]
