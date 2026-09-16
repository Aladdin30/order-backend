"""Tenancy, authentication, user branch scoping, and physical table models."""

from __future__ import annotations

import uuid
from decimal import Decimal
from typing import TYPE_CHECKING

from sqlalchemy import (
    Boolean,
    Enum as SAEnum,
    ForeignKey,
    Index,
    Numeric,
    SmallInteger,
    String,
    UniqueConstraint,
)
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.models.base import Base, LocalizedText, TimestampMixin, UUIDPrimaryKeyMixin
from app.models.enums import TableStatus, UserRole

if TYPE_CHECKING:
    from app.models.catalog import Category
    from app.models.kitchen_station import KitchenStation
    from app.models.order import Order, Payment
    from app.models.service import Review, ServiceRequest


class Tenant(Base, UUIDPrimaryKeyMixin, TimestampMixin):
    """Top-level organizational tenant (brand/restaurant group)."""

    __tablename__ = "tenants"

    name: Mapped[str] = mapped_column(String(255), nullable=False)
    slug: Mapped[str] = mapped_column(String(100), unique=True, index=True, nullable=False)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)

    # Relationships
    branches: Mapped[list[Branch]] = relationship(
        "Branch",
        back_populates="tenant",
        cascade="all, delete-orphan",
        passive_deletes=True,
    )
    users: Mapped[list[User]] = relationship(
        "User",
        back_populates="tenant",
        cascade="all, delete-orphan",
        passive_deletes=True,
    )
    orders: Mapped[list[Order]] = relationship(
        "Order",
        back_populates="tenant",
    )


class Branch(Base, UUIDPrimaryKeyMixin, TimestampMixin):
    """Physical restaurant branch with spatial geofencing attributes."""

    __tablename__ = "branches"

    tenant_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("tenants.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    name: Mapped[LocalizedText] = mapped_column(JSONB, nullable=False)
    slug: Mapped[str] = mapped_column(String(100), nullable=False)
    latitude: Mapped[Decimal] = mapped_column(Numeric(10, 7), nullable=False)
    longitude: Mapped[Decimal] = mapped_column(Numeric(10, 7), nullable=False)
    geofence_radius_meters: Mapped[int] = mapped_column(
        SmallInteger,
        default=150,
        nullable=False,
    )
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)

    __table_args__ = (
        Index("ix_branches_tenant_id_slug", "tenant_id", "slug", unique=True),
    )

    # Relationships
    tenant: Mapped[Tenant] = relationship("Tenant", back_populates="branches")
    user_access: Mapped[list[UserBranchAccess]] = relationship(
        "UserBranchAccess",
        back_populates="branch",
        cascade="all, delete-orphan",
        passive_deletes=True,
    )
    assigned_users: Mapped[list[User]] = relationship(
        "User",
        secondary="user_branch_access",
        viewonly=True,
    )
    tables: Mapped[list[Table]] = relationship(
        "Table",
        back_populates="branch",
        cascade="all, delete-orphan",
        passive_deletes=True,
    )
    categories: Mapped[list[Category]] = relationship(
        "Category",
        back_populates="branch",
        cascade="all, delete-orphan",
        passive_deletes=True,
    )
    orders: Mapped[list[Order]] = relationship(
        "Order",
        back_populates="branch",
    )
    service_requests: Mapped[list[ServiceRequest]] = relationship(
        "ServiceRequest",
        back_populates="branch",
        cascade="all, delete-orphan",
        passive_deletes=True,
    )
    reviews: Mapped[list[Review]] = relationship(
        "Review",
        back_populates="branch",
        cascade="all, delete-orphan",
        passive_deletes=True,
    )
    kitchen_stations: Mapped[list[KitchenStation]] = relationship(
        "KitchenStation",
        back_populates="branch",
        cascade="all, delete-orphan",
        passive_deletes=True,
    )


class User(Base, UUIDPrimaryKeyMixin, TimestampMixin):
    """Staff and administrative users scoped to a tenant."""

    __tablename__ = "users"

    tenant_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("tenants.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    email: Mapped[str] = mapped_column(String(255), unique=True, index=True, nullable=False)
    hashed_password: Mapped[str] = mapped_column(String(255), nullable=False)
    full_name: Mapped[str] = mapped_column(String(255), nullable=False)
    role: Mapped[UserRole] = mapped_column(
        SAEnum(UserRole, name="user_role", native_enum=True),
        nullable=False,
    )
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)

    # Relationships
    tenant: Mapped[Tenant] = relationship("Tenant", back_populates="users")
    branch_access: Mapped[list[UserBranchAccess]] = relationship(
        "UserBranchAccess",
        back_populates="user",
        cascade="all, delete-orphan",
        passive_deletes=True,
    )
    assigned_branches: Mapped[list[Branch]] = relationship(
        "Branch",
        secondary="user_branch_access",
        viewonly=True,
    )
    verified_payments: Mapped[list[Payment]] = relationship(
        "Payment",
        back_populates="verified_by_user",
    )


class UserBranchAccess(Base, UUIDPrimaryKeyMixin, TimestampMixin):
    """Explicit M:N branch scoping association table for regional & branch staff."""

    __tablename__ = "user_branch_access"

    user_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("users.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    branch_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("branches.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )

    __table_args__ = (
        UniqueConstraint("user_id", "branch_id", name="uq_user_branch_access"),
    )

    # Relationships
    user: Mapped[User] = relationship("User", back_populates="branch_access")
    branch: Mapped[Branch] = relationship("Branch", back_populates="user_access")


class Table(Base, UUIDPrimaryKeyMixin, TimestampMixin):
    """Physical dining tables within a specific branch."""

    __tablename__ = "tables"

    branch_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("branches.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    table_number: Mapped[str] = mapped_column(String(50), nullable=False)
    capacity: Mapped[int] = mapped_column(SmallInteger, nullable=False)
    status: Mapped[TableStatus] = mapped_column(
        SAEnum(TableStatus, name="table_status", native_enum=True),
        default=TableStatus.AVAILABLE,
        nullable=False,
    )
    current_session_token: Mapped[str | None] = mapped_column(String(255), nullable=True)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)

    __table_args__ = (
        UniqueConstraint("branch_id", "table_number", name="uq_tables_branch_table_number"),
    )

    # Relationships
    branch: Mapped[Branch] = relationship("Branch", back_populates="tables")
    orders: Mapped[list[Order]] = relationship("Order", back_populates="table")
    service_requests: Mapped[list[ServiceRequest]] = relationship(
        "ServiceRequest",
        back_populates="table",
        cascade="all, delete-orphan",
        passive_deletes=True,
    )
