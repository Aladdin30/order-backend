"""Pydantic schemas for Brand and nested Branch administration."""

from __future__ import annotations

import uuid
from datetime import datetime
from decimal import Decimal
from typing import Any

from pydantic import BaseModel, ConfigDict, Field


class BranchCreateNested(BaseModel):
    """Payload for creating a branch nested under a brand."""

    model_config = ConfigDict(extra="forbid")

    name: dict[str, str] = Field(
        ...,
        description="Localized name mapping, e.g. {'en': 'City Center', 'ar': 'وسط البلد'}",
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


class BrandCreate(BaseModel):
    """Payload for creating a brand, with optional atomic branch creation."""

    model_config = ConfigDict(extra="forbid")

    name: str = Field(..., min_length=2, max_length=120, description="Brand legal / trade name")
    slug: str = Field(..., min_length=2, max_length=120, description="Unique brand slug")
    is_active: bool = Field(default=True)

    # Optional atomic onboarding: single-location or multi-branch setup
    default_branch: BranchCreateNested | None = Field(
        default=None,
        description="Optional default branch for atomic single-location onboarding",
    )
    branches: list[BranchCreateNested] | None = Field(
        default=None,
        description="Optional list of branches for atomic multi-branch setup",
    )


class BrandUpdate(BaseModel):
    """Partial update payload for a brand."""

    model_config = ConfigDict(extra="forbid")

    name: str | None = Field(default=None, min_length=2, max_length=120)
    slug: str | None = Field(default=None, min_length=2, max_length=120)
    is_active: bool | None = Field(default=None)


class BranchSummaryResponse(BaseModel):
    """Summary branch view nested inside brand responses."""

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
    is_active: bool
    created_at: datetime


class BrandResponse(BaseModel):
    """Authoritative representation of a brand and its operational branches."""

    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    name: str
    slug: str
    is_active: bool
    branches: list[BranchSummaryResponse] = Field(default_factory=list)
    created_at: datetime
    updated_at: datetime


class BrandListResponse(BaseModel):
    """Paginated collection of brands."""

    total: int
    items: list[BrandResponse]
