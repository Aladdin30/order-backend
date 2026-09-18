"""Pydantic schemas for cash drawer shift management and End-of-Day Z-Report."""

from __future__ import annotations

import uuid
from datetime import date, datetime
from decimal import Decimal

from pydantic import BaseModel, ConfigDict, Field

from app.models.enums import DrawerStatus


# ---------------------------------------------------------------------------
# Cash Drawer Schemas
# ---------------------------------------------------------------------------

class DrawerOpenRequest(BaseModel):
    """Payload to open a new cash drawer shift."""

    opening_balance: Decimal = Field(
        default=Decimal("0.00"),
        ge=Decimal("0.00"),
        description="Starting cash float in drawer (must be >= 0)",
    )


class DrawerCloseRequest(BaseModel):
    """Payload to close an active cash drawer shift and declare physical cash."""

    declared_cash_amount: Decimal = Field(
        ...,
        ge=Decimal("0.00"),
        description="Physically counted cash in drawer at end of shift",
    )
    closing_notes: str | None = Field(
        default=None,
        description="Optional cashier/manager shift closing remarks or incident notes",
    )


class CashDrawerSessionResponse(BaseModel):
    """Detailed response schema for a cash drawer shift session."""

    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    branch_id: uuid.UUID
    opened_by_user_id: uuid.UUID
    closed_by_user_id: uuid.UUID | None = None
    status: DrawerStatus
    opening_balance: Decimal
    declared_cash_amount: Decimal | None = None
    calculated_cash_amount: Decimal | None = None
    cash_variance: Decimal | None = None
    opened_at: datetime
    closed_at: datetime | None = None
    closing_notes: str | None = None
    created_at: datetime
    updated_at: datetime


# ---------------------------------------------------------------------------
# Financial Breakdown Value Objects
# ---------------------------------------------------------------------------

class PaymentChannelBreakdown(BaseModel):
    """Sales partitioned across tender types."""

    cash: Decimal = Decimal("0.00")
    card_pos: Decimal = Decimal("0.00")
    online: Decimal = Decimal("0.00")


class OrderSourceBreakdown(BaseModel):
    """Sales partitioned across originating channels."""

    qr_customer: Decimal = Decimal("0.00")
    cashier_pos: Decimal = Decimal("0.00")
    takeaway_app: Decimal = Decimal("0.00")


class OrderVolumeCounters(BaseModel):
    """Aggregated volume counts of orders within the period."""

    total_orders: int = 0
    paid_orders: int = 0
    refunded_orders: int = 0
    cancelled_orders: int = 0


class CashReconciliationSnapshot(BaseModel):
    """Snapshot of linked cash drawer figures at closing."""

    opening_balance: Decimal | None = None
    declared_cash: Decimal | None = None
    cash_variance: Decimal | None = None


# ---------------------------------------------------------------------------
# Z-Report Schemas
# ---------------------------------------------------------------------------

class ZReportGenerateRequest(BaseModel):
    """Optional parameters for triggering an End-of-Day Z-Report."""

    drawer_session_id: uuid.UUID | None = Field(
        default=None,
        description="Optional cash drawer session ID to link and snapshot",
    )
    period_start: datetime | None = Field(
        default=None,
        description="Custom start boundary (UTC); defaults to business day start or drawer open",
    )
    period_end: datetime | None = Field(
        default=None,
        description="Custom end boundary (UTC); defaults to current time",
    )


class ZReportResponse(BaseModel):
    """Immutable End-of-Day financial closing summary."""

    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    report_number: str
    branch_id: uuid.UUID
    generated_by_user_id: uuid.UUID
    drawer_session_id: uuid.UUID | None = None
    business_date: date
    period_start: datetime
    period_end: datetime

    # Financial buckets
    gross_sales: Decimal
    net_sales: Decimal
    total_tax: Decimal
    total_service_fees: Decimal
    total_discounts: Decimal
    total_refunds: Decimal

    # Breakdowns & Counters
    payment_channels: PaymentChannelBreakdown
    order_sources: OrderSourceBreakdown
    order_volumes: OrderVolumeCounters
    cash_reconciliation: CashReconciliationSnapshot

    created_at: datetime


class ZReportListResponse(BaseModel):
    """Paginated list of historical Z-Reports."""

    total: int
    items: list[ZReportResponse]
