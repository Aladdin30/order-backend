"""Guest table service requests and post-dining review models."""

from __future__ import annotations

import datetime
import uuid
from typing import TYPE_CHECKING

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    DateTime,
    Enum as SAEnum,
    ForeignKey,
    Index,
    SmallInteger,
    String,
    Text,
)
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship, synonym

from app.models.base import Base, TimestampMixin, UUIDPrimaryKeyMixin
from app.models.enums import ServiceRequestStatus, ServiceRequestType

if TYPE_CHECKING:
    from app.models.auth import Branch, Table
    from app.models.order import Order


class ServiceRequest(Base, UUIDPrimaryKeyMixin, TimestampMixin):
    """Guest table-side service call (water, napkins, waiter call, etc.)."""

    __tablename__ = "service_requests"

    branch_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("branches.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    table_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("tables.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    request_type: Mapped[ServiceRequestType] = mapped_column(
        SAEnum(ServiceRequestType, name="service_request_type", native_enum=True),
        nullable=False,
    )
    status: Mapped[ServiceRequestStatus] = mapped_column(
        SAEnum(ServiceRequestStatus, name="service_request_status", native_enum=True),
        default=ServiceRequestStatus.PENDING,
        nullable=False,
    )
    note: Mapped[str | None] = mapped_column(String(255), default=None, nullable=True)
    is_escalated: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    escalated_at: Mapped[datetime.datetime | None] = mapped_column(
        DateTime(timezone=True),
        default=None,
        nullable=True,
    )
    acknowledged_at: Mapped[datetime.datetime | None] = mapped_column(
        DateTime(timezone=True),
        default=None,
        nullable=True,
    )
    completed_at: Mapped[datetime.datetime | None] = mapped_column(
        DateTime(timezone=True),
        default=None,
        nullable=True,
    )
    dismissed_at: Mapped[datetime.datetime | None] = mapped_column(
        DateTime(timezone=True),
        default=None,
        nullable=True,
    )

    escalated = synonym("is_escalated")

    __table_args__ = (
        Index("ix_service_requests_branch_status", "branch_id", "status"),
        Index("ix_service_requests_sla_pending", "status", "is_escalated", "created_at"),
    )

    # Relationships
    branch: Mapped[Branch] = relationship("Branch", back_populates="service_requests")
    table: Mapped[Table] = relationship("Table", back_populates="service_requests")


class Review(Base, UUIDPrimaryKeyMixin, TimestampMixin):
    """Post-meal customer feedback with rating check constraint and manager resolution."""

    __tablename__ = "reviews"

    branch_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("branches.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    order_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("orders.id", ondelete="CASCADE"),
        unique=True,
        nullable=False,
        index=True,
    )
    rating: Mapped[int] = mapped_column(SmallInteger, nullable=False)
    comment: Mapped[str | None] = mapped_column(Text, nullable=True)
    escalated_to_manager: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    manager_resolution_note: Mapped[str | None] = mapped_column(Text, nullable=True)

    __table_args__ = (
        CheckConstraint("rating >= 1 AND rating <= 5", name="ck_reviews_rating_range"),
        Index("ix_reviews_branch_rating", "branch_id", "rating"),
    )

    # Relationships
    branch: Mapped[Branch] = relationship("Branch", back_populates="reviews")
    order: Mapped[Order] = relationship("Order", back_populates="review")
