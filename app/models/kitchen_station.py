"""Dynamic kitchen station entity model for multi-station KDS routing."""

from __future__ import annotations

import uuid
from typing import TYPE_CHECKING, ClassVar

from sqlalchemy import (
    Boolean,
    ForeignKey,
    Index,
    String,
    UniqueConstraint,
)
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.models.base import Base, LocalizedText, TimestampMixin, UUIDPrimaryKeyMixin

if TYPE_CHECKING:
    from app.models.auth import Branch, Tenant
    from app.models.catalog import Category, Item


class KitchenStation(Base, UUIDPrimaryKeyMixin, TimestampMixin):
    """Dynamic physical or operational kitchen station belonging to a branch."""

    __tablename__ = "kitchen_stations"

    # Static constants for backward-compatible reference
    HOT_KITCHEN: ClassVar[str] = "HOT_KITCHEN"
    COLD_KITCHEN: ClassVar[str] = "COLD_KITCHEN"
    BEVERAGE: ClassVar[str] = "BEVERAGE"

    tenant_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("tenants.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    branch_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("branches.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    name: Mapped[LocalizedText] = mapped_column(JSONB, nullable=False)
    code: Mapped[str] = mapped_column(String(64), nullable=False)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)

    __table_args__ = (
        UniqueConstraint("branch_id", "code", name="uq_kitchen_station_branch_code"),
        Index("ix_kitchen_stations_branch_active", "branch_id", "is_active"),
    )

    # Relationships
    branch: Mapped[Branch] = relationship("Branch", back_populates="kitchen_stations")
    tenant: Mapped[Tenant] = relationship("Tenant")
    items: Mapped[list[Item]] = relationship(
        "Item",
        back_populates="kitchen_station",
        foreign_keys="Item.station_id",
    )
    categories: Mapped[list[Category]] = relationship(
        "Category",
        back_populates="kitchen_station",
        foreign_keys="Category.station_id",
    )


# Model alias for semantic clarity
KitchenStationModel = KitchenStation
