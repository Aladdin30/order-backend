"""Pydantic schemas for branch spatial location, geofencing, and dynamic financials."""

from __future__ import annotations

import uuid
from datetime import datetime
from decimal import Decimal
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

# Re-export for cross-schema convenience
from app.schemas.brand import BranchSummaryResponse  # noqa: F401


class UpdateBranchLocationRequest(BaseModel):
    """Payload to update physical branch GPS coordinates and geofencing radius in meters."""

    model_config = ConfigDict(extra="forbid")

    latitude: Decimal = Field(
        ...,
        ge=Decimal("-90.0"),
        le=Decimal("90.0"),
        description="Branch latitude in degrees (-90.0 to 90.0)",
    )
    longitude: Decimal = Field(
        ...,
        ge=Decimal("-180.0"),
        le=Decimal("180.0"),
        description="Branch longitude in degrees (-180.0 to 180.0)",
    )
    geofence_radius_meters: int = Field(
        ...,
        ge=5,
        le=5000,
        description="Allowed spatial geofence radius in meters (5m to 5000m)",
    )


class BranchLocationResponse(BaseModel):
    """Authoritative response confirming updated branch coordinates and geofence radius."""

    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    tenant_id: uuid.UUID
    name: dict[str, str] | Any
    slug: str
    latitude: Decimal
    longitude: Decimal
    geofence_radius_meters: int
    is_active: bool


class UpdateBranchFinancialSettingsRequest(BaseModel):
    """Payload to update branch dynamic taxes and service charge parameters."""

    model_config = ConfigDict(extra="forbid")

    tax_rate: Decimal = Field(
        ...,
        ge=Decimal("0.0000"),
        le=Decimal("1.0000"),
        description="VAT or sales tax rate as decimal fraction (e.g. 0.1500 for 15%)",
    )
    service_fee_rate: Decimal = Field(
        ...,
        ge=Decimal("0.0000"),
        le=Decimal("1.0000"),
        description="Service charge rate as decimal fraction (e.g. 0.1200 for 12%)",
    )
    is_service_taxable: bool = Field(
        default=False,
        description="When true, tax is applied over (Subtotal + Service Fee). When false, tax is applied to Subtotal only.",
    )
    is_tax_inclusive: bool = Field(
        default=False,
        description="Whether menu catalog pricing includes VAT.",
    )
    service_fee_dine_in_only: bool = Field(
        default=True,
        description="When true, service charge automatically drops to 0.00 for takeaway orders.",
    )


class BranchFinancialSettingsResponse(BaseModel):
    """Authoritative financial profile configuration for a branch."""

    model_config = ConfigDict(from_attributes=True)

    branch_id: uuid.UUID
    tax_rate: Decimal
    service_fee_rate: Decimal
    is_service_taxable: bool
    is_tax_inclusive: bool
    service_fee_dine_in_only: bool


class BranchCreateRequest(BaseModel):
    """Payload to create a new branch."""

    model_config = ConfigDict(extra="forbid")

    brand_id: uuid.UUID | None = Field(default=None, description="Optional brand ID")
    name: dict[str, str] = Field(
        ...,
        description="Localized branch name mapping, e.g. {'en': 'Downtown', 'ar': 'وسط البلد'}",
    )
    slug: str = Field(..., min_length=2, max_length=100)
    currency: str = Field(default="EGP", min_length=3, max_length=3)
    timezone: str = Field(default="Africa/Cairo", max_length=50)
    latitude: Decimal = Field(default=Decimal("30.0444"), ge=Decimal("-90.0"), le=Decimal("90.0"))
    longitude: Decimal = Field(default=Decimal("31.2357"), ge=Decimal("-180.0"), le=Decimal("180.0"))
    geofence_radius_meters: int = Field(default=150, ge=5, le=5000)
    tax_rate: Decimal = Field(default=Decimal("0.0000"), ge=Decimal("0.0000"), le=Decimal("1.0000"))
    service_fee_rate: Decimal = Field(default=Decimal("0.0000"), ge=Decimal("0.0000"), le=Decimal("1.0000"))
    is_service_taxable: bool = Field(default=False)
    is_tax_inclusive: bool = Field(default=False)
    service_fee_dine_in_only: bool = Field(default=True)
    is_active: bool = Field(default=True)


class BranchUpdateRequest(BaseModel):
    """Payload for partial updates to branch settings."""

    model_config = ConfigDict(extra="forbid")

    name: dict[str, str] | None = None
    slug: str | None = Field(default=None, min_length=2, max_length=100)
    brand_id: uuid.UUID | None = None
    currency: str | None = Field(default=None, min_length=3, max_length=3)
    timezone: str | None = Field(default=None, max_length=50)
    is_active: bool | None = None


class BranchDetailResponse(BaseModel):
    """Comprehensive branch details."""

    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    tenant_id: uuid.UUID
    brand_id: uuid.UUID | None = None
    name: dict[str, str] | Any
    slug: str
    currency: str
    timezone: str
    latitude: Decimal
    longitude: Decimal
    geofence_radius_meters: int
    tax_rate: Decimal
    service_fee_rate: Decimal
    is_service_taxable: bool
    is_tax_inclusive: bool
    service_fee_dine_in_only: bool
    is_active: bool
    created_at: datetime
    updated_at: datetime

