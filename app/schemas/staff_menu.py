"""Pydantic v2 schemas for staff menu catalog administration and Item 86 toggle."""

from __future__ import annotations

import datetime
import uuid
from decimal import Decimal

from pydantic import BaseModel, ConfigDict, Field

from app.models.enums import KitchenStation


# ---------------------------------------------------------------------------
# Category Schemas
# ---------------------------------------------------------------------------


class StaffCategoryCreate(BaseModel):
    """Payload for creating a new menu category."""

    name: dict[str, str] = Field(
        ...,
        description="Bilingual category name (e.g. {'en': 'Burgers', 'ar': 'برغر'})",
        min_length=1,
    )
    display_order: int = Field(default=0, ge=0, description="Display sequence order index")
    station: KitchenStation = Field(
        default=KitchenStation.HOT_KITCHEN,
        description="Kitchen fulfillment station",
    )
    station_id: uuid.UUID | None = Field(default=None, description="Optional custom dynamic kitchen station ID")
    is_active: bool = Field(default=True, description="Whether category is active and visible")


class StaffCategoryUpdate(BaseModel):
    """Payload for updating an existing menu category."""

    name: dict[str, str] | None = Field(default=None, description="Updated bilingual name")
    display_order: int | None = Field(default=None, ge=0, description="Updated sequence order")
    station: KitchenStation | None = Field(default=None, description="Updated fulfillment station")
    station_id: uuid.UUID | None = Field(default=None, description="Updated dynamic kitchen station ID")
    is_active: bool | None = Field(default=None, description="Updated active status")


class StaffCategoryResponse(BaseModel):
    """Staff view of a category record."""

    id: uuid.UUID
    branch_id: uuid.UUID
    name: dict[str, str]
    display_order: int
    station: KitchenStation
    station_id: uuid.UUID | None = None
    is_active: bool
    created_at: datetime.datetime
    updated_at: datetime.datetime

    model_config = ConfigDict(from_attributes=True)


# ---------------------------------------------------------------------------
# Item Schemas
# ---------------------------------------------------------------------------


class StaffItemCreate(BaseModel):
    """Payload for creating a new catalog item."""

    category_id: uuid.UUID = Field(..., description="Target category UUID")
    name: dict[str, str] = Field(
        ...,
        description="Bilingual item name (e.g. {'en': 'Burger', 'ar': 'برغر'})",
        min_length=1,
    )
    description: dict[str, str] | None = Field(
        default=None,
        description="Bilingual item description",
    )
    base_price: Decimal = Field(..., ge=Decimal("0.00"), description="Base price in SAR")
    station: KitchenStation | None = Field(
        default=None,
        description="Target kitchen station override (inherits from category if None)",
    )
    station_id: uuid.UUID | None = Field(default=None, description="Optional custom dynamic kitchen station ID")
    image_url: str | None = Field(default=None, max_length=1024, description="CDN image URL")
    is_available: bool = Field(
        default=True,
        description="Item 86 availability status (False = sold out / grayed out)",
    )
    allergens: list[str] = Field(default_factory=list, description="Allergen classification tags")
    dietary_badges: list[str] = Field(default_factory=list, description="Dietary badges")


class StaffItemUpdate(BaseModel):
    """Payload for updating an existing catalog item."""

    category_id: uuid.UUID | None = Field(default=None, description="Move item to new category")
    name: dict[str, str] | None = Field(default=None, description="Updated bilingual name")
    description: dict[str, str] | None = Field(default=None, description="Updated bilingual description")
    base_price: Decimal | None = Field(default=None, ge=Decimal("0.00"), description="Updated base price")
    station: KitchenStation | None = Field(default=None, description="Updated kitchen station")
    station_id: uuid.UUID | None = Field(default=None, description="Updated dynamic kitchen station ID")
    image_url: str | None = Field(default=None, max_length=1024, description="Updated image URL")
    is_available: bool | None = Field(default=None, description="Item 86 toggle")
    allergens: list[str] | None = Field(default=None, description="Updated allergens")
    dietary_badges: list[str] | None = Field(default=None, description="Updated dietary badges")


class StaffItemAvailabilityUpdate(BaseModel):
    """Payload for instant Item 86 toggle."""

    is_available: bool = Field(
        ...,
        description="Instant 86 switch: False = 86'd (sold out), True = available",
    )


class StaffItemResponse(BaseModel):
    """Staff view of an item record."""

    id: uuid.UUID
    category_id: uuid.UUID
    name: dict[str, str]
    description: dict[str, str] | None = None
    base_price: Decimal
    station: KitchenStation | None = None
    station_id: uuid.UUID | None = None
    image_url: str | None = None
    is_available: bool
    allergens: list[str]
    dietary_badges: list[str]
    created_at: datetime.datetime
    updated_at: datetime.datetime

    model_config = ConfigDict(from_attributes=True)


# ---------------------------------------------------------------------------
# Modifier Group & Option Schemas
# ---------------------------------------------------------------------------


class StaffModifierGroupCreate(BaseModel):
    """Payload for creating a modifier group for an item."""

    name: dict[str, str] = Field(
        ...,
        description="Bilingual modifier group name (e.g. {'en': 'Size', 'ar': 'الحجم'})",
        min_length=1,
    )
    min_choices: int = Field(default=0, ge=0, description="Minimum selections required")
    max_choices: int = Field(default=1, ge=1, description="Maximum selections allowed")
    is_required: bool = Field(default=False, description="Mandatory selection flag")


class StaffModifierGroupUpdate(BaseModel):
    """Payload for updating a modifier group."""

    name: dict[str, str] | None = Field(default=None, description="Updated bilingual name")
    min_choices: int | None = Field(default=None, ge=0, description="Updated min choices")
    max_choices: int | None = Field(default=None, ge=1, description="Updated max choices")
    is_required: bool | None = Field(default=None, description="Updated mandatory flag")


class StaffModifierGroupResponse(BaseModel):
    """Staff view of a modifier group record."""

    id: uuid.UUID
    item_id: uuid.UUID
    name: dict[str, str]
    min_choices: int
    max_choices: int
    is_required: bool
    created_at: datetime.datetime
    updated_at: datetime.datetime

    model_config = ConfigDict(from_attributes=True)


class StaffModifierOptionCreate(BaseModel):
    """Payload for adding an option to a modifier group."""

    name: dict[str, str] = Field(
        ...,
        description="Bilingual option name (e.g. {'en': 'Double Patty', 'ar': 'شريحة مضاعفة'})",
        min_length=1,
    )
    price_delta: Decimal = Field(default=Decimal("0.00"), description="Price addition or deduction in SAR")
    is_available: bool = Field(default=True, description="Option availability status")


class StaffModifierOptionUpdate(BaseModel):
    """Payload for updating a modifier option."""

    name: dict[str, str] | None = Field(default=None, description="Updated bilingual name")
    price_delta: Decimal | None = Field(default=None, description="Updated price delta")
    is_available: bool | None = Field(default=None, description="Updated availability")


class StaffModifierOptionAvailabilityUpdate(BaseModel):
    """Payload for toggling modifier option availability."""

    is_available: bool = Field(..., description="Option availability flag (86 toggle)")


class StaffModifierOptionResponse(BaseModel):
    """Staff view of a modifier option record."""

    id: uuid.UUID
    modifier_group_id: uuid.UUID
    name: dict[str, str]
    price_delta: Decimal
    is_available: bool
    created_at: datetime.datetime
    updated_at: datetime.datetime

    model_config = ConfigDict(from_attributes=True)
