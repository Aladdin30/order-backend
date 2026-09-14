"""Declarative base and core mixins for SQLAlchemy 2.0+ models."""

import datetime
import uuid
from typing import TypeAlias

from sqlalchemy import DateTime, func
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

# Type alias representing bilingual JSONB payload: {"en": "...", "ar": "..."}
LocalizedText: TypeAlias = dict[str, str]


class Base(DeclarativeBase):
    """Root declarative base class for all domain entities."""

    pass


class TimestampMixin:
    """Abstract mixin providing automatic timezone-aware audit timestamps."""

    created_at: Mapped[datetime.datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        nullable=False,
    )
    updated_at: Mapped[datetime.datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        onupdate=func.now(),
        nullable=False,
    )


class UUIDPrimaryKeyMixin:
    """Abstract mixin providing a PostgreSQL native UUID primary key with client default."""

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        primary_key=True,
        default=uuid.uuid4,
    )
