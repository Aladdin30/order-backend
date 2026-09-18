"""Pydantic v2 schemas for localized menu catalog retrieval and modifier validation engine."""

from __future__ import annotations

import uuid
from decimal import Decimal
from typing import Any

from pydantic import AliasChoices, BaseModel, ConfigDict, Field

from app.models.enums import KitchenStation, MenuItemScope
from app.schemas.i18n import LocalizedStr, OptionalLocalizedStr


# ---------------------------------------------------------------------------
# Catalog Tree Read Models (Zero-Overhead Dynamic Localization via LocalizedStr)
# ---------------------------------------------------------------------------

class ModifierOptionResponse(BaseModel):
    """Modifier option item with localized label, price adjustment, and 86 availability status."""

    id: uuid.UUID
    modifier_group_id: uuid.UUID
    name: LocalizedStr = Field(..., description="Dynamically resolved localized option name")
    price_delta: Decimal = Field(default=Decimal("0.00"), description="Price addition or deduction")
    is_available: bool = Field(..., description="Option 86 switch flag")

    model_config = ConfigDict(from_attributes=True)


class ModifierGroupResponse(BaseModel):
    """Modifier group specification enforcing selection boundaries and mandatory flags."""

    id: uuid.UUID
    item_id: uuid.UUID
    name: LocalizedStr = Field(..., description="Dynamically resolved localized group name")
    min_choices: int = Field(default=0, ge=0, description="Minimum allowed option choices")
    max_choices: int = Field(default=1, ge=1, description="Maximum allowed option choices")
    is_required: bool = Field(default=False, description="Mandatory selection flag")
    options: list[ModifierOptionResponse] = Field(
        default_factory=list,
        description="Available modifier options",
    )

    model_config = ConfigDict(from_attributes=True)


class MenuItemResponse(BaseModel):
    """Catalog item with pricing, station routing, allergens, and nested modifier groups."""

    id: uuid.UUID
    category_id: uuid.UUID
    name: LocalizedStr = Field(..., description="Dynamically resolved localized item name")
    description: OptionalLocalizedStr = Field(
        default=None,
        description="Dynamically resolved localized item description or null",
    )
    base_price: Decimal = Field(..., description="Base unit price before modifiers")
    station: KitchenStation | None = Field(default=None, description="Target kitchen station for prep routing")
    image_url: str | None = Field(default=None, description="CDN or storage URL for item imagery")
    is_available: bool = Field(..., description="Item 86 switch flag (sold out if False)")
    allergens: list[str] = Field(default_factory=list, description="Allergen classification tags")
    dietary_badges: list[str] = Field(default_factory=list, description="Dietary badges (e.g. Vegan, Halal)")
    modifier_groups: list[ModifierGroupResponse] = Field(
        default_factory=list,
        description="Customization modifier groups",
    )

    model_config = ConfigDict(from_attributes=True)


class MenuCategoryResponse(BaseModel):
    """Catalog category grouping items with sequence order."""

    id: uuid.UUID
    branch_id: uuid.UUID
    name: LocalizedStr = Field(..., description="Dynamically resolved localized category name")
    display_order: int = Field(default=0, description="Presentation sequence index")
    station: KitchenStation = Field(default=KitchenStation.HOT_KITCHEN, description="Default kitchen fulfillment station")
    is_active: bool = Field(..., description="Category active status")
    items: list[MenuItemResponse] = Field(
        default_factory=list,
        description="Items belonging to this category",
    )

    model_config = ConfigDict(from_attributes=True)


class MenuTreeResponse(BaseModel):
    """Complete hierarchical catalog tree for a specific branch."""

    branch_id: uuid.UUID = Field(..., description="Target branch identifier")
    categories: list[MenuCategoryResponse] = Field(
        default_factory=list,
        description="Active menu categories in display order",
    )

    model_config = ConfigDict(from_attributes=True)


# ---------------------------------------------------------------------------
# Modifier Validation & Pricing Calculation Schemas
# ---------------------------------------------------------------------------

class SelectedModifierGroupInput(BaseModel):
    """User selections for a single modifier group."""

    group_id: uuid.UUID = Field(..., description="Modifier group UUID")
    option_ids: list[uuid.UUID] = Field(
        default_factory=list,
        description="Selected modifier option UUIDs within this group",
    )


class ValidateItemSelectionRequest(BaseModel):
    """Incoming request to authoritatively validate item configuration and calculate pricing."""

    item_id: uuid.UUID = Field(..., description="Target catalog item UUID")
    quantity: int = Field(default=1, ge=1, le=100, description="Item quantity to validate")
    selected_groups: list[SelectedModifierGroupInput] = Field(
        default_factory=list,
        validation_alias=AliasChoices("selected_groups", "selections", "selected_modifiers", "modifiers"),
        description="List of modifier group selections",
    )
    special_instructions: str | None = Field(
        default=None,
        max_length=500,
        description="Optional customer preparation notes",
    )
    table_id: uuid.UUID | None = Field(
        default=None,
        description="Optional table identifier (overrides session when authorized)",
    )
    client_session_id: str | None = Field(
        default=None,
        max_length=128,
        description="Optional client session identifier for cart item attribution",
    )
    guest_label: str | None = Field(
        default=None,
        max_length=100,
        description="Optional guest name/label (e.g. 'Guest 1')",
    )

    model_config = ConfigDict(populate_by_name=True)


class SelectedModifierOptionSnapshot(BaseModel):
    """Immutable modifier option snapshot item ready for JSONB storage in order_items."""

    group_id: uuid.UUID = Field(..., description="Modifier group UUID")
    group_name: str = Field(..., description="Resolved localized modifier group name")
    option_id: uuid.UUID = Field(..., description="Modifier option UUID")
    name: str = Field(..., description="Resolved localized modifier option name")
    price_delta: Decimal = Field(..., description="Price delta applied by this option")

    model_config = ConfigDict(from_attributes=True)


class ValidatedItemSelectionResponse(BaseModel):
    """Authoritative server-side validated item configuration and calculated financial totals."""

    item_id: uuid.UUID = Field(..., description="Catalog item UUID")
    item_name: str = Field(..., description="Resolved localized item name")
    quantity: int = Field(..., description="Validated item quantity")
    base_price: Decimal = Field(..., description="Item base price")
    unit_price: Decimal = Field(..., description="Effective unit price (base_price + sum(price_delta))")
    subtotal: Decimal = Field(..., description="Authoritative subtotal (unit_price * quantity)")
    station: KitchenStation = Field(..., description="Kitchen station for prep routing")
    selected_modifiers: list[SelectedModifierOptionSnapshot] = Field(
        default_factory=list,
        description="Immutable snapshot array of selected modifiers for JSONB storage",
    )
    table_id: uuid.UUID = Field(..., description="Canonical dining table UUID")
    client_session_id: str | None = Field(
        default=None,
        description="Client session identifier for shared cart attribution",
    )
    guest_label: str | None = Field(
        default=None,
        description="Guest label for shared cart attribution",
    )
    special_instructions: str | None = Field(
        default=None,
        description="Special preparation instructions",
    )

    model_config = ConfigDict(from_attributes=True)


# ---------------------------------------------------------------------------
# Scoped Menu & Branch Override Schemas
# ---------------------------------------------------------------------------


class BranchMenuItemResponse(BaseModel):
    """Catalog item resolved with branch-specific overrides."""

    id: uuid.UUID
    category_id: uuid.UUID
    category_name: LocalizedStr | None = None
    name: LocalizedStr
    description: OptionalLocalizedStr = None
    base_price: Decimal
    final_price: Decimal
    scope: MenuItemScope = MenuItemScope.ALL_BRANCHES
    is_available: bool
    is_visible: bool = True
    has_override: bool = False
    price_override: Decimal | None = None
    image_url: str | None = None
    allergens: list[str] = Field(default_factory=list)
    dietary_badges: list[str] = Field(default_factory=list)

    model_config = ConfigDict(from_attributes=True)


class BranchMenuCategoryGroup(BaseModel):
    """Category grouping for branch menu display."""

    category_id: uuid.UUID
    category_name: LocalizedStr
    display_order: int = 0
    items: list[BranchMenuItemResponse] = Field(default_factory=list)

    model_config = ConfigDict(from_attributes=True)


class BranchMenuResponse(BaseModel):
    """Complete effective branch menu with categories and overridden items."""

    branch_id: uuid.UUID
    brand_id: uuid.UUID | None = None
    currency: str = "EGP"
    categories: list[BranchMenuCategoryGroup] = Field(default_factory=list)

    model_config = ConfigDict(from_attributes=True)


class BranchMenuOverrideUpdate(BaseModel):
    """Payload for branch admin to modify branch-specific price or availability."""

    price_override: Decimal | None = None
    is_available: bool | None = None
    is_visible: bool | None = None


class ScopedItemCreateRequest(BaseModel):
    """Payload for creating a brand-level catalog item with scope."""

    name: dict[str, str]
    description: dict[str, str] | None = None
    base_price: Decimal
    category_id: uuid.UUID
    brand_id: uuid.UUID | None = None
    scope: MenuItemScope = MenuItemScope.ALL_BRANCHES
    target_branch_ids: list[uuid.UUID] | None = None
    station_id: uuid.UUID | None = None
    image_url: str | None = None

