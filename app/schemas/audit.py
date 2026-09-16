"""Pydantic schemas for audit logs representation and query filtering."""

import datetime
import uuid
from typing import Any

from pydantic import BaseModel, ConfigDict, Field


class AuditLogResponse(BaseModel):
    """Detailed audit log entry schema."""

    id: uuid.UUID
    tenant_id: uuid.UUID
    branch_id: uuid.UUID | None = None
    user_id: uuid.UUID | None = None
    actor_role: str | None = None
    action: str
    resource_type: str
    resource_id: str | None = None
    ip_address: str | None = None
    user_agent: str | None = None
    changes: dict[str, Any] = Field(default_factory=dict)
    status: str
    created_at: datetime.datetime

    model_config = ConfigDict(from_attributes=True)


class AuditLogListResponse(BaseModel):
    """Paginated collection of audit log entries."""

    total: int
    items: list[AuditLogResponse]
