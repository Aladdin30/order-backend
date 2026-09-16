"""Pydantic v2 schemas for table-side service requests and staff dispatch workflows."""

from __future__ import annotations

import datetime
import uuid
from typing import Self

from pydantic import BaseModel, ConfigDict, Field, model_validator

from app.models.enums import ServiceRequestStatus, ServiceRequestType


class CreateServiceRequest(BaseModel):
    """Guest input payload for opening a table-side service request."""

    request_type: ServiceRequestType = Field(
        ...,
        description="Category of service request (WATER, CUTLERY, WAITER_CALL, PACK_LEFTOVERS, TAKEAWAY_ORDER, OTHER)",
    )
    note: str | None = Field(
        default=None,
        max_length=200,
        description="Optional customer note; mandatory (3-200 chars) for request_type OTHER",
    )

    @model_validator(mode="after")
    def validate_note_for_other(self) -> Self:
        clean_note = self.note.strip() if self.note is not None else None
        if self.request_type == ServiceRequestType.OTHER:
            if not clean_note or len(clean_note) < 3:
                raise ValueError("A note description (3-200 characters) is required when request_type is OTHER")
            self.note = clean_note
        else:
            self.note = clean_note
        return self


class UpdateServiceRequestStatus(BaseModel):
    """Staff payload for transitioning a service request status."""

    status: ServiceRequestStatus = Field(
        ...,
        description="Target lifecycle status (ACKNOWLEDGED, COMPLETED, DISMISSED)",
    )


class ServiceRequestResponse(BaseModel):
    """Public representation of a table service request."""

    id: uuid.UUID
    branch_id: uuid.UUID
    table_id: uuid.UUID
    table_number: str | None = Field(
        default=None,
        description="Assigned table number for floor and runner routing",
    )
    request_type: ServiceRequestType
    status: ServiceRequestStatus
    note: str | None = None
    escalated: bool = False
    created_at: datetime.datetime
    acknowledged_at: datetime.datetime | None = None
    completed_at: datetime.datetime | None = None
    dismissed_at: datetime.datetime | None = None

    model_config = ConfigDict(from_attributes=True)
