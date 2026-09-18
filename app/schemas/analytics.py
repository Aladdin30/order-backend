"""Pydantic schemas for cross-branch analytics, executive KPIs, and Menu Engineering."""

from __future__ import annotations

import uuid
from datetime import datetime
from decimal import Decimal
from enum import StrEnum

from pydantic import BaseModel, ConfigDict, Field


class TimePeriod(StrEnum):
    """Supported analytics reporting horizons."""

    TODAY = "today"
    YESTERDAY = "yesterday"
    LAST_7_DAYS = "last_7_days"
    LAST_30_DAYS = "last_30_days"
    CUSTOM = "custom"


# ---------------------------------------------------------------------------
# Item & Category Performance
# ---------------------------------------------------------------------------

class ItemPerformanceItem(BaseModel):
    """Analytical metrics for an individual menu item."""

    model_config = ConfigDict(from_attributes=True)

    item_id: uuid.UUID
    item_name: str | dict[str, str]
    category_name: str | dict[str, str] | None = None
    total_quantity_sold: int = 0
    gross_revenue: Decimal = Decimal("0.00")
    unit_price: Decimal = Decimal("0.00")
    cancellations_count: int = 0
    sales_contribution_percentage: Decimal = Decimal("0.00")


class CategoryPerformanceItem(BaseModel):
    """Aggregate metrics across a menu category."""

    model_config = ConfigDict(from_attributes=True)

    category_id: uuid.UUID
    category_name: str | dict[str, str]
    total_items_sold: int = 0
    total_revenue: Decimal = Decimal("0.00")
    gmv_share_percentage: Decimal = Decimal("0.00")


class MenuPerformanceResponse(BaseModel):
    """Dedicated response model for Menu Engineering."""

    period_start: datetime
    period_end: datetime
    top_selling_items: list[ItemPerformanceItem]
    bottom_selling_items: list[ItemPerformanceItem]
    category_breakdown: list[CategoryPerformanceItem]


# ---------------------------------------------------------------------------
# Branch Performance Matrix
# ---------------------------------------------------------------------------

class BranchPerformanceRow(BaseModel):
    """Comparative operational and revenue metrics per branch."""

    model_config = ConfigDict(from_attributes=True)

    branch_id: uuid.UUID
    branch_name: str | dict[str, str]
    gmv: Decimal = Decimal("0.00")
    total_paid_orders: int = 0
    aov: Decimal = Decimal("0.00")
    cancellation_rate_percentage: Decimal = Decimal("0.00")
    cash_revenue: Decimal = Decimal("0.00")
    digital_revenue: Decimal = Decimal("0.00")


class BranchesMatrixResponse(BaseModel):
    """Matrix of all branches ranked by gross merchandise value."""

    period_start: datetime
    period_end: datetime
    branches: list[BranchPerformanceRow]


# ---------------------------------------------------------------------------
# Executive KPI Summary & Consolidated Dashboard
# ---------------------------------------------------------------------------

class ExecutiveKPISummary(BaseModel):
    """High-level platform/branch financial health indicators."""

    gmv: Decimal = Decimal("0.00")
    net_revenue: Decimal = Decimal("0.00")
    total_tax: Decimal = Decimal("0.00")
    total_service_fees: Decimal = Decimal("0.00")
    total_discounts: Decimal = Decimal("0.00")
    total_refunds: Decimal = Decimal("0.00")
    total_orders: int = 0
    paid_orders: int = 0
    cancelled_orders: int = 0
    aov: Decimal = Decimal("0.00")
    average_items_per_order: Decimal = Decimal("0.00")


class DashboardConsolidatedResponse(BaseModel):
    """Consolidated executive dashboard integrating KPIs, menu, and branch rankings."""

    period_start: datetime
    period_end: datetime
    kpis: ExecutiveKPISummary
    top_selling_items: list[ItemPerformanceItem]
    bottom_selling_items: list[ItemPerformanceItem]
    category_breakdown: list[CategoryPerformanceItem]
    branch_rankings: list[BranchPerformanceRow] = []
