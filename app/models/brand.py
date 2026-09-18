"""Brand organizational model for hierarchical multi-tenancy."""

from __future__ import annotations

import uuid
from typing import TYPE_CHECKING

from sqlalchemy import Boolean, String
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.models.base import Base, TimestampMixin, UUIDPrimaryKeyMixin

if TYPE_CHECKING:
    from app.models.auth import Branch, User


class Brand(Base, UUIDPrimaryKeyMixin, TimestampMixin):
    """Hierarchical Brand organization owning multiple branches and catalog items."""

    __tablename__ = "brands"

    name: Mapped[str] = mapped_column(String(120), nullable=False)
    slug: Mapped[str] = mapped_column(String(120), unique=True, index=True, nullable=False)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, server_default="true", nullable=False)

    # Relationships
    branches: Mapped[list[Branch]] = relationship(
        "Branch",
        back_populates="brand",
        cascade="all, delete-orphan",
        passive_deletes=True,
    )
    users: Mapped[list[User]] = relationship(
        "User",
        back_populates="brand",
    )
