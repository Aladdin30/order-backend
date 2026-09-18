"""Scoped menu catalog models and branch-level overrides."""

from __future__ import annotations

import uuid
from decimal import Decimal
from typing import TYPE_CHECKING

from sqlalchemy import Boolean, ForeignKey, Numeric, UniqueConstraint
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.models.base import Base, TimestampMixin, UUIDPrimaryKeyMixin
from app.models.catalog import Item
from app.models.enums import MenuItemScope

if TYPE_CHECKING:
    from app.models.auth import Branch

# Convenience alias
MenuItem = Item


class BranchMenuOverride(Base, UUIDPrimaryKeyMixin, TimestampMixin):
    """Branch-level menu overrides for pricing, 86 availability, and visibility."""

    __tablename__ = "branch_menu_overrides"

    branch_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("branches.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    menu_item_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("items.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    price_override: Mapped[Decimal | None] = mapped_column(
        Numeric(10, 2),
        nullable=True,
        default=None,
    )
    is_available: Mapped[bool] = mapped_column(
        Boolean,
        default=True,
        server_default="true",
        nullable=False,
    )
    is_visible: Mapped[bool] = mapped_column(
        Boolean,
        default=True,
        server_default="true",
        nullable=False,
    )

    __table_args__ = (
        UniqueConstraint("branch_id", "menu_item_id", name="uq_branch_menu_overrides"),
    )

    # Relationships
    branch: Mapped[Branch] = relationship("Branch", back_populates="menu_overrides")
    menu_item: Mapped[Item] = relationship("Item", back_populates="branch_overrides")


__all__ = ["MenuItemScope", "MenuItem", "BranchMenuOverride"]
