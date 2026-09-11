"""Menu catalog, categories, items, and modifier models."""

from __future__ import annotations

import uuid
from decimal import Decimal
from typing import TYPE_CHECKING

from sqlalchemy import (
    Boolean,
    Enum as SAEnum,
    ForeignKey,
    Numeric,
    SmallInteger,
    String,
)
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.models.base import Base, LocalizedText, TimestampMixin, UUIDPrimaryKeyMixin
from app.models.enums import KitchenStation

if TYPE_CHECKING:
    from app.models.auth import Branch
    from app.models.order import OrderItem


class Category(Base, UUIDPrimaryKeyMixin, TimestampMixin):
    """Menu category scoped to a branch (e.g. Appetizers, Mains, Drinks)."""

    __tablename__ = "categories"

    branch_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("branches.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    name: Mapped[LocalizedText] = mapped_column(JSONB, nullable=False)
    display_order: Mapped[int] = mapped_column(SmallInteger, default=0, nullable=False)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)

    # Relationships
    branch: Mapped[Branch] = relationship("Branch", back_populates="categories")
    items: Mapped[list[Item]] = relationship(
        "Item",
        back_populates="category",
        cascade="all, delete-orphan",
        passive_deletes=True,
    )


class Item(Base, UUIDPrimaryKeyMixin, TimestampMixin):
    """Catalog item with pricing, kitchen routing, allergen badges, and 86 toggle."""

    __tablename__ = "items"

    category_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("categories.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    name: Mapped[LocalizedText] = mapped_column(JSONB, nullable=False)
    description: Mapped[LocalizedText | None] = mapped_column(JSONB, nullable=True)
    base_price: Mapped[Decimal] = mapped_column(Numeric(10, 2), nullable=False)
    station: Mapped[KitchenStation] = mapped_column(
        SAEnum(KitchenStation, name="kitchen_station", native_enum=True),
        nullable=False,
    )
    image_url: Mapped[str | None] = mapped_column(String(1024), nullable=True)
    is_available: Mapped[bool] = mapped_column(
        Boolean,
        default=True,
        nullable=False,
        doc="Item 86 switch to instantly 86 an item when stock runs out.",
    )
    allergens: Mapped[list[str]] = mapped_column(
        JSONB,
        default=list,
        nullable=False,
    )
    dietary_badges: Mapped[list[str]] = mapped_column(
        JSONB,
        default=list,
        nullable=False,
    )

    # Relationships
    category: Mapped[Category] = relationship("Category", back_populates="items")
    modifier_groups: Mapped[list[ModifierGroup]] = relationship(
        "ModifierGroup",
        back_populates="item",
        cascade="all, delete-orphan",
        passive_deletes=True,
    )
    order_items: Mapped[list[OrderItem]] = relationship(
        "OrderItem",
        back_populates="item",
    )


class ModifierGroup(Base, UUIDPrimaryKeyMixin, TimestampMixin):
    """Customization modifier group for an item (e.g. Choice of Sauce, Meat Doneness)."""

    __tablename__ = "modifier_groups"

    item_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("items.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    name: Mapped[LocalizedText] = mapped_column(JSONB, nullable=False)
    min_choices: Mapped[int] = mapped_column(SmallInteger, default=0, nullable=False)
    max_choices: Mapped[int] = mapped_column(SmallInteger, default=1, nullable=False)
    is_required: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)

    # Relationships
    item: Mapped[Item] = relationship("Item", back_populates="modifier_groups")
    options: Mapped[list[ModifierOption]] = relationship(
        "ModifierOption",
        back_populates="modifier_group",
        cascade="all, delete-orphan",
        passive_deletes=True,
    )


class ModifierOption(Base, UUIDPrimaryKeyMixin, TimestampMixin):
    """Individual option inside a modifier group with price delta adjustment."""

    __tablename__ = "modifier_options"

    modifier_group_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("modifier_groups.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    name: Mapped[LocalizedText] = mapped_column(JSONB, nullable=False)
    price_delta: Mapped[Decimal] = mapped_column(
        Numeric(10, 2),
        default=Decimal("0.00"),
        nullable=False,
    )
    is_available: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)

    # Relationships
    modifier_group: Mapped[ModifierGroup] = relationship("ModifierGroup", back_populates="options")
