"""Pydantic schemas for dynamic kitchen stations management."""

from __future__ import annotations

import datetime
import re
import uuid
from typing import Annotated

from pydantic import BaseModel, ConfigDict, Field, field_validator

from app.models.base import LocalizedText


class CreateKitchenStationRequest(BaseModel):
    """Payload for creating a custom branch kitchen station."""

    name: LocalizedText = Field(
        ...,
        description="Bilingual localized station name, e.g. {'en': 'Pizza Oven', 'ar': 'فرن البيتزا'}",
    )
    code: str = Field(
        ...,
        min_length=2,
        max_length=50,
        description="Alphanumeric station code/slug, e.g. PIZZA_OVEN, BAR, GRILL",
    )
    is_active: bool = Field(
        default=True,
        description="Whether the station is active for receiving order tickets",
    )

    @field_validator("code")
    @classmethod
    def validate_and_uppercase_code(cls, v: str) -> str:
        clean = v.strip().upper()
        if not re.match(r"^[A-Z0-9_]+$", clean):
            raise ValueError("Station code must contain only uppercase letters, numbers, and underscores.")
        return clean


class UpdateKitchenStationRequest(BaseModel):
    """Payload for updating an existing kitchen station."""

    name: LocalizedText | None = Field(
        default=None,
        description="Updated localized station name",
    )
    is_active: bool | None = Field(
        default=None,
        description="Toggle active status of the station",
    )


class KitchenStationResponse(BaseModel):
    """Response representation of a kitchen station."""

    id: uuid.UUID
    branch_id: uuid.UUID
    tenant_id: uuid.UUID
    name: LocalizedText
    code: str
    is_active: bool
    created_at: datetime.datetime
    updated_at: datetime.datetime

    model_config = ConfigDict(from_attributes=True)
