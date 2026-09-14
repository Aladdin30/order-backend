"""Pydantic schemas for Cryptographic QR Signature Engine."""

from __future__ import annotations

import uuid
from decimal import Decimal

from pydantic import BaseModel, ConfigDict, Field


class QRTokenPayload(BaseModel):
    """Strongly-typed decoded physical table QR token payload."""

    tenant_id: uuid.UUID
    branch_id: uuid.UUID
    table_id: uuid.UUID
    key_version: int = Field(..., ge=1, description="Signing key version identifier")

    model_config = ConfigDict(frozen=True)


class QRVerifyRequest(BaseModel):
    """Incoming request payload for table QR verification."""

    token: str = Field(..., min_length=1, description="Compact URL-safe physical QR token")


class QRVerificationResponse(BaseModel):
    """Public verification response with physical table and branch metadata."""

    is_valid: bool = Field(True, description="Indicates successful cryptographic integrity and active DB verification")
    tenant_id: uuid.UUID
    branch_id: uuid.UUID
    table_id: uuid.UUID
    table_number: str
    branch_name: dict[str, str] = Field(..., description="Multilingual branch name dictionary")
    branch_latitude: Decimal | float
    branch_longitude: Decimal | float
    geofence_radius_meters: int
    requires_geofence_check: bool = Field(True, description="Signals client requirement to enforce device location validation")

    model_config = ConfigDict(from_attributes=True)


class QRGenerateTokenRequest(BaseModel):
    """Staff request payload to generate a signed physical table QR token."""

    table_id: uuid.UUID = Field(..., description="Physical table UUID")
    key_version: int = Field(1, ge=1, description="Signing key version (defaults to active version)")


class QRGenerateTokenResponse(BaseModel):
    """Response returned upon generating a signed physical table QR token."""

    token: str = Field(..., description="Compact URL-safe physical QR token ready for sticker printing")
    tenant_id: uuid.UUID
    branch_id: uuid.UUID
    table_id: uuid.UUID
    table_number: str

    model_config = ConfigDict(from_attributes=True)
