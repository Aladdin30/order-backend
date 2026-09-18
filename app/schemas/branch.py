"""Pydantic schemas for branch spatial location, geofencing, and dynamic financials."""

from __future__ import annotations

import uuid
from decimal import Decimal
from typing import Any

from pydantic import BaseModel, ConfigDict, Field


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
