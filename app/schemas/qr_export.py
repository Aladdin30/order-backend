"""Pydantic schemas for cryptographic QR export and batch generation engine."""

from __future__ import annotations

import uuid
from datetime import datetime
from enum import StrEnum

from pydantic import BaseModel, ConfigDict, Field


class QRExportFormat(StrEnum):
    """Supported export media formats for table QR codes."""

    PNG = "png"
    SVG = "svg"


class SingleTableQRResponse(BaseModel):
    """Metadata response containing cryptographically signed client URL for a table."""

    table_id: uuid.UUID
    table_number: str
    branch_id: uuid.UUID
    signed_url: str
    signature: str
    timestamp: int

    model_config = ConfigDict(from_attributes=True)


class BatchQRExportRequest(BaseModel):
    """Request payload for exporting multiple table QR codes in an in-memory ZIP package."""

    table_ids: list[uuid.UUID] | None = Field(
        default=None,
        description="Optional list of table UUIDs to export. If omitted, exports all active tables in branch.",
    )
    format: QRExportFormat = Field(
        default=QRExportFormat.PNG,
        description="Asset format: 'png' (raster) or 'svg' (vector).",
    )
    scale: int = Field(
        default=10,
        ge=1,
        le=50,
        description="Scale factor / box size for QR modules.",
    )
    include_label: bool = Field(
        default=True,
        description="Whether to render the physical table label text below the QR code.",
    )

    model_config = ConfigDict(from_attributes=True)


class BatchQRExportManifest(BaseModel):
    """Manifest JSON embedded inside the generated batch ZIP archive."""

    branch_id: uuid.UUID
    total_tables: int
    format: QRExportFormat
    files: list[str]
    generated_at: datetime

    model_config = ConfigDict(from_attributes=True)


class QRVerificationProbeResponse(BaseModel):
    """Response returned upon successful cryptographic signature verification probe."""

    valid: bool = True
    table_id: uuid.UUID
    branch_id: uuid.UUID
    table_number: str
    is_active: bool
    timestamp: int

    model_config = ConfigDict(from_attributes=True)
