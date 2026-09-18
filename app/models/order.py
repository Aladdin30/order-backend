"""Operational financial ledgers: orders, order items, and payment transactions."""

from __future__ import annotations

import datetime
import uuid
from decimal import Decimal
from typing import TYPE_CHECKING, Any

from sqlalchemy import (
    Boolean,
    DateTime,
    Enum as SAEnum,
    ForeignKey,
    Index,
    Numeric,
    SmallInteger,
    String,
    Text,
)
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.models.base import Base, TimestampMixin, UUIDPrimaryKeyMixin
from app.models.enums import (
    KitchenStation,
    OrderSource,
    OrderStatus,
    OrderType,
    PaymentMethod,
    PaymentStatus,
)

if TYPE_CHECKING:
    from app.models.auth import Branch, Table, Tenant, User
    from app.models.catalog import Item
    from app.models.service import Review


class Order(Base, UUIDPrimaryKeyMixin, TimestampMixin):
    """Customer order ledger with strict financial FK protection."""

    __tablename__ = "orders"

    tenant_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("tenants.id", ondelete="RESTRICT"),
        nullable=False,
        index=True,
    )
    branch_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("branches.id", ondelete="RESTRICT"),
        nullable=False,
        index=True,
    )
    table_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("tables.id", ondelete="RESTRICT"),
        nullable=True,
        index=True,
    )
    status: Mapped[OrderStatus] = mapped_column(
        SAEnum(OrderStatus, name="order_status", native_enum=True),
        default=OrderStatus.DRAFT,
        nullable=False,
    )
    order_type: Mapped[OrderType] = mapped_column(
        SAEnum(OrderType, name="order_type", native_enum=True),
        default=OrderType.DINE_IN,
        nullable=False,
    )
    order_source: Mapped[OrderSource] = mapped_column(
        SAEnum(OrderSource, name="order_source", native_enum=True),
        default=OrderSource.QR_CUSTOMER,
        server_default="QR_CUSTOMER",
        nullable=False,
    )
    created_by_user_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("users.id", ondelete="SET NULL"),
        nullable=True,
        index=True,
    )
    pickup_number: Mapped[int | None] = mapped_column(
        SmallInteger,
        nullable=True,
        index=True,
    )
    subtotal: Mapped[Decimal] = mapped_column(
        Numeric(10, 2),
        default=Decimal("0.00"),
        nullable=False,
    )
    service_fee_rate: Mapped[Decimal] = mapped_column(
        Numeric(5, 4),
        default=Decimal("0.0000"),
        server_default="0.0000",
        nullable=False,
    )
    service_fee_total: Mapped[Decimal] = mapped_column(
        Numeric(10, 2),
        default=Decimal("0.00"),
        server_default="0.00",
        nullable=False,
    )
    applied_tax_rate: Mapped[Decimal] = mapped_column(
        Numeric(5, 4),
        default=Decimal("0.0000"),
        server_default="0.0000",
        nullable=False,
    )
    tax_total: Mapped[Decimal] = mapped_column(
        Numeric(10, 2),
        default=Decimal("0.00"),
        nullable=False,
    )
    total_amount: Mapped[Decimal] = mapped_column(
        Numeric(10, 2),
        default=Decimal("0.00"),
        nullable=False,
    )
    is_paid: Mapped[bool] = mapped_column(Boolean, default=False, server_default="false", nullable=False)
    customer_notes: Mapped[str | None] = mapped_column(Text, nullable=True)
    cancellation_reason: Mapped[str | None] = mapped_column(Text, nullable=True)

    __table_args__ = (
        Index("ix_orders_branch_status", "branch_id", "status"),
        Index("ix_orders_branch_created_at", "branch_id", "created_at"),
    )

    # Relationships
    tenant: Mapped[Tenant] = relationship("Tenant", back_populates="orders")
    branch: Mapped[Branch] = relationship("Branch", back_populates="orders")
    table: Mapped[Table | None] = relationship("Table", back_populates="orders")
    created_by_user: Mapped[User | None] = relationship("User", foreign_keys=[created_by_user_id])
    order_items: Mapped[list[OrderItem]] = relationship(
        "OrderItem",
        back_populates="order",
        cascade="all, delete-orphan",
        passive_deletes=True,
    )
    payments: Mapped[list[Payment]] = relationship(
        "Payment",
        back_populates="order",
    )
    review: Mapped[Review | None] = relationship(
        "Review",
        back_populates="order",
        uselist=False,
    )


class OrderItem(Base, UUIDPrimaryKeyMixin, TimestampMixin):
    """Line item within an order containing modifier snapshots and kitchen station routing."""

    __tablename__ = "order_items"

    order_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("orders.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    item_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("items.id", ondelete="RESTRICT"),
        nullable=False,
        index=True,
    )
    quantity: Mapped[int] = mapped_column(SmallInteger, default=1, nullable=False)
    unit_price: Mapped[Decimal] = mapped_column(Numeric(10, 2), nullable=False)
    subtotal: Mapped[Decimal] = mapped_column(Numeric(10, 2), nullable=False)
    station_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("kitchen_stations.id", ondelete="SET NULL"),
        nullable=True,
        index=True,
    )
    station_code: Mapped[str | None] = mapped_column(String(64), nullable=True)
    station: Mapped[KitchenStation] = mapped_column(
        SAEnum(KitchenStation, name="kitchen_station", native_enum=True),
        default=KitchenStation.HOT_KITCHEN,
        nullable=False,
    )
    is_bumped: Mapped[bool] = mapped_column(
        Boolean,
        default=False,
        nullable=False,
        doc="KDS bump bar status indicating whether preparation has finished at this station.",
    )
    selected_modifiers: Mapped[list[dict[str, Any]]] = mapped_column(
        JSONB,
        default=list,
        nullable=False,
        doc="Immutable snapshot array capturing modifier selections and price deltas at checkout.",
    )
    special_instructions: Mapped[str | None] = mapped_column(String(500), nullable=True)

    # Relationships
    order: Mapped[Order] = relationship("Order", back_populates="order_items")
    item: Mapped[Item] = relationship("Item", back_populates="order_items")


class Payment(Base, UUIDPrimaryKeyMixin, TimestampMixin):
    """Immutable transaction settlement ledger."""

    __tablename__ = "payments"

    tenant_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("tenants.id", ondelete="CASCADE"),
        nullable=True,
        index=True,
    )
    branch_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("branches.id", ondelete="CASCADE"),
        nullable=True,
        index=True,
    )
    order_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("orders.id", ondelete="RESTRICT"),
        nullable=False,
        index=True,
    )
    payment_method: Mapped[PaymentMethod] = mapped_column(
        SAEnum(PaymentMethod, name="payment_method", native_enum=True),
        nullable=False,
    )
    amount: Mapped[Decimal] = mapped_column(Numeric(10, 2), nullable=False)
    currency: Mapped[str] = mapped_column(String(3), default="SAR", nullable=False)
    status: Mapped[PaymentStatus] = mapped_column(
        SAEnum(PaymentStatus, name="payment_status", native_enum=True),
        default=PaymentStatus.PENDING,
        nullable=False,
    )
    transaction_reference: Mapped[str | None] = mapped_column(String(255), nullable=True, index=True)
    idempotency_key: Mapped[str | None] = mapped_column(String(255), unique=True, nullable=True, index=True)
    verified_by_user_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("users.id", ondelete="SET NULL"),
        nullable=True,
        index=True,
    )
    verified_at: Mapped[datetime.datetime | None] = mapped_column(
        DateTime(timezone=True),
        default=None,
        nullable=True,
    )

    @property
    def method(self) -> PaymentMethod:
        """Alias for payment_method."""
        return self.payment_method

    # Relationships
    order: Mapped[Order] = relationship("Order", back_populates="payments")
    verified_by_user: Mapped[User | None] = relationship(
        "User",
        back_populates="verified_payments",
    )
