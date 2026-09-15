"""Schemas and contexts for Table Session & Presence Verification."""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from typing import Self

from pydantic import BaseModel, ConfigDict, Field, model_validator

from app.models.enums import TableStatus
from app.schemas.i18n import LocalizedStr


class TablePresenceVerifyRequest(BaseModel):
    """Payload for guest presence verification and session issuance."""

    qr_token: str = Field(
        ...,
        min_length=1,
        description="Physical HMAC-SHA256 signed table QR token",
    )
    client_latitude: float | None = Field(
        default=None,
        description="Client device latitude (-90.0 to 90.0)",
    )
    client_longitude: float | None = Field(
        default=None,
        description="Client device longitude (-180.0 to 180.0)",
    )

    model_config = ConfigDict(frozen=True)

    @model_validator(mode="after")
    def validate_coordinate_pair(self) -> Self:
        """Enforce that coordinates are either both provided or both omitted."""
        has_lat = self.client_latitude is not None
        has_lon = self.client_longitude is not None

        if has_lat != has_lon:
            raise ValueError(
                "Both client_latitude and client_longitude must be provided together, or both omitted."
            )

        if has_lat:
            assert self.client_latitude is not None and self.client_longitude is not None
            if not (-90.0 <= self.client_latitude <= 90.0):
                raise ValueError("client_latitude must be between -90.0 and 90.0 degrees.")
            if not (-180.0 <= self.client_longitude <= 180.0):
                raise ValueError("client_longitude must be between -180.0 and 180.0 degrees.")

        return self


class TableSessionResponse(BaseModel):
    """Structured response containing verified guest session JWT and metadata."""

    session_id: uuid.UUID = Field(..., description="Unique dining session identifier")
    session_token: str = Field(..., description="Cryptographically signed Guest Session JWT")
    token_type: str = Field(default="bearer", description="Token type format")
    tenant_id: uuid.UUID = Field(..., description="Tenant UUID")
    branch_id: uuid.UUID = Field(..., description="Branch UUID")
    branch_name: LocalizedStr = Field(..., description="Localized branch name")
    table_id: uuid.UUID = Field(..., description="Table UUID")
    table_number: str = Field(..., description="Physical table number")
    table_status: TableStatus = Field(..., description="Current operational table status")
    is_presence_verified: bool = Field(
        ...,
        description="True if client presence was verified within branch geofence boundary",
    )
    computed_distance_meters: float | None = Field(
        default=None,
        description="Calculated Haversine distance in meters (None if coordinates omitted)",
    )
    expires_in: int = Field(..., description="Token validity window in seconds")

    model_config = ConfigDict(from_attributes=True)


@dataclass(frozen=True)
class GuestSessionContext:
    """Immutable security context constructed from a validated Guest Session JWT."""

    session_id: uuid.UUID
    tenant_id: uuid.UUID
    branch_id: uuid.UUID
    table_id: uuid.UUID
    table_number: str
    is_presence_verified: bool
