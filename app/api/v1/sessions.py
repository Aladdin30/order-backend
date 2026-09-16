"""Table Session & Presence Verification Endpoints."""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, Request
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import RequirePresenceVerified, get_async_db, get_current_guest_session
from app.core.rate_limit import rate_limit
from app.schemas.session import (
    GuestSessionContext,
    TablePresenceVerifyRequest,
    TableSessionResponse,
)
from app.services.session_service import SessionService

router = APIRouter(prefix="/sessions", tags=["sessions"])


@router.post(
    "/verify-presence",
    response_model=TableSessionResponse,
    summary="Verify Table Presence and Issue Guest Session",
    description=(
        "Public endpoint accepting a physical HMAC-SHA256 table QR token and optional GPS coordinates. "
        "Enforces the three-tier presence policy, transitions table state to BROWSING, "
        "and issues a short-lived Guest Session JWT."
    ),
)
@rate_limit(max_requests=30, window_seconds=60)
async def verify_table_presence(
    request: Request,
    body: TablePresenceVerifyRequest,
    db: Annotated[AsyncSession, Depends(get_async_db)],
) -> TableSessionResponse:
    """Guest presence verification and session issuance pipeline."""
    return await SessionService.verify_presence_and_issue_session(db, body)


@router.get(
    "/me",
    summary="Get Current Guest Session Context",
    description="Inspect claims and presence verification status of the active guest session JWT.",
)
async def get_guest_session_profile(
    session: Annotated[GuestSessionContext, Depends(get_current_guest_session)],
) -> dict:
    """Retrieve verified guest session context."""
    return {
        "session_id": session.session_id,
        "tenant_id": session.tenant_id,
        "branch_id": session.branch_id,
        "table_id": session.table_id,
        "table_number": session.table_number,
        "is_presence_verified": session.is_presence_verified,
    }


@router.post(
    "/verified-action",
    summary="Demonstration Action Requiring Verified Physical Presence",
    description="Enforces RequirePresenceVerified dependency, blocking unverified (Tier 3) sessions.",
)
async def execute_presence_verified_action(
    session: Annotated[GuestSessionContext, Depends(RequirePresenceVerified)],
) -> dict:
    """Protected action strictly requiring verified in-branch presence."""
    return {
        "status": "success",
        "message": "Action permitted: presence verified within branch geofence.",
        "session_id": session.session_id,
        "table_number": session.table_number,
    }
