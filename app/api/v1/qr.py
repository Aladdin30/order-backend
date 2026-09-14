"""Cryptographic Table QR endpoints: public verification and staff token generation."""

from __future__ import annotations

import uuid
from typing import Annotated

from fastapi import APIRouter, Body, Depends, HTTPException, Query, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import EnforceBranchAccess, RequireRoles, get_async_db
from app.core.context import SecurityContext
from app.core.qr_security import (
    InvalidTokenError,
    TokenTamperedError,
    UnsupportedKeyVersionError,
)
from app.models.enums import UserRole
from app.schemas.qr import (
    QRGenerateTokenRequest,
    QRGenerateTokenResponse,
    QRVerificationResponse,
    QRVerifyRequest,
)
from app.services.qr_service import QRService

router = APIRouter(prefix="/qr", tags=["qr"])


@router.post(
    "/verify",
    response_model=QRVerificationResponse,
    summary="Verify Physical Table QR Token",
    description="Public endpoint to cryptographically verify a physical table QR token and resolve active branch/table metadata.",
)
async def verify_table_qr_token(
    request: QRVerifyRequest,
    db: Annotated[AsyncSession, Depends(get_async_db)],
) -> QRVerificationResponse:
    """Public verification pipeline for dining guests scanning physical QR stickers."""
    try:
        return await QRService.verify_table_qr(db, request.token)
    except (InvalidTokenError, TokenTamperedError, UnsupportedKeyVersionError) as exc:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=str(exc),
        ) from exc


@router.post(
    "/generate-token",
    response_model=QRGenerateTokenResponse,
    summary="Generate Physical Table QR Token",
    description="Staff-protected endpoint to issue a tamper-proof table QR token for physical sticker printing.",
)
async def generate_table_qr_token(
    db: Annotated[AsyncSession, Depends(get_async_db)],
    context: Annotated[
        SecurityContext,
        Depends(RequireRoles([UserRole.SUPER_ADMIN, UserRole.BRANCH_ADMIN])),
    ],
    branch_id: Annotated[uuid.UUID, Depends(EnforceBranchAccess())],
    table_id: uuid.UUID | None = Query(None, description="Physical Table UUID"),
    body: QRGenerateTokenRequest | None = Body(None),
) -> QRGenerateTokenResponse:
    """Issue signed table QR token scoped to tenant and branch."""
    target_table_id = body.table_id if body and body.table_id else table_id
    if not target_table_id:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="table_id must be provided via request body or query parameter.",
        )

    key_version = body.key_version if body and body.key_version else 1

    return await QRService.generate_table_token(
        db=db,
        tenant_id=context.tenant_id,
        branch_id=branch_id,
        table_id=target_table_id,
        key_version=key_version,
    )
