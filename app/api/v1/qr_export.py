"""QR Export API: Dynamic cryptographic QR single and batch export endpoints."""

from __future__ import annotations

import uuid
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Query, Response, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import EnforceBranchAccess, get_async_db, require_roles
from app.models.auth import Table, User
from app.models.enums import UserRole
from app.schemas.qr_export import (
    BatchQRExportRequest,
    QRExportFormat,
    QRVerificationProbeResponse,
    SingleTableQRResponse,
)
from app.services.qr_generator_service import QRGeneratorService

router = APIRouter(tags=["qr-export"])

QR_EXPORT_ROLES = [
    UserRole.BRANCH_ADMIN,
    UserRole.SUPER_ADMIN,
]


@router.get(
    "/tables/{table_id}/image",
    summary="Stream dynamic QR code image for a single table",
    status_code=status.HTTP_200_OK,
)
async def export_single_table_image(
    table_id: uuid.UUID,
    db: Annotated[AsyncSession, Depends(get_async_db)],
    branch_id: Annotated[uuid.UUID, Depends(EnforceBranchAccess())],
    current_user: Annotated[User, Depends(require_roles(QR_EXPORT_ROLES))],
    format: Annotated[QRExportFormat, Query()] = QRExportFormat.PNG,
    scale: Annotated[int, Query(ge=1, le=50)] = 10,
    include_label: Annotated[bool, Query()] = True,
) -> Response:
    """Generate and stream single table QR code asset in PNG or SVG format."""
    stmt = select(Table).where(
        Table.id == table_id,
        Table.branch_id == branch_id,
        Table.is_active.is_(True),
    )
    result = await db.execute(stmt)
    table = result.scalar_one_or_none()

    if not table:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Table not found within specified branch.",
        )

    signed_url, _, _, _ = QRGeneratorService.create_signed_payload(table.id, branch_id)
    img_bytes, media_type = QRGeneratorService.render_table_qr(
        signed_url=signed_url,
        table_number=table.table_number,
        export_format=format,
        scale=scale,
        include_label=include_label,
    )

    filename = QRGeneratorService.sanitize_filename(table.table_number, format, pad=False)
    return Response(
        content=img_bytes,
        media_type=media_type,
        headers={
            "Content-Disposition": f'inline; filename="{filename}"',
        },
    )


@router.get(
    "/tables/{table_id}/url",
    response_model=SingleTableQRResponse,
    summary="Generate signed URL metadata for a single table",
    status_code=status.HTTP_200_OK,
)
async def get_single_table_qr_url(
    table_id: uuid.UUID,
    db: Annotated[AsyncSession, Depends(get_async_db)],
    branch_id: Annotated[uuid.UUID, Depends(EnforceBranchAccess())],
    current_user: Annotated[User, Depends(require_roles(QR_EXPORT_ROLES))],
) -> SingleTableQRResponse:
    """Retrieve cryptographic signed URL, signature, and timestamp for a table."""
    stmt = select(Table).where(
        Table.id == table_id,
        Table.branch_id == branch_id,
        Table.is_active.is_(True),
    )
    result = await db.execute(stmt)
    table = result.scalar_one_or_none()

    if not table:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Table not found within specified branch.",
        )

    signed_url, signature, ts, _ = QRGeneratorService.create_signed_payload(table.id, branch_id)
    return SingleTableQRResponse(
        table_id=table.id,
        table_number=table.table_number,
        branch_id=branch_id,
        signed_url=signed_url,
        signature=signature,
        timestamp=ts,
    )


@router.post(
    "/batch",
    summary="Export batch of table QR codes in an in-memory ZIP package",
    status_code=status.HTTP_200_OK,
)
async def export_batch_qr(
    payload: BatchQRExportRequest,
    db: Annotated[AsyncSession, Depends(get_async_db)],
    branch_id: Annotated[uuid.UUID, Depends(EnforceBranchAccess())],
    current_user: Annotated[User, Depends(require_roles(QR_EXPORT_ROLES))],
) -> Response:
    """Generate and stream an in-memory ZIP archive of table QR codes with manifest.json."""
    zip_bytes, filename = await QRGeneratorService.export_branch_qr_batch(
        db=db,
        branch_id=branch_id,
        table_ids=payload.table_ids,
        export_format=payload.format,
        scale=payload.scale,
        include_label=payload.include_label,
    )

    return Response(
        content=zip_bytes,
        media_type="application/zip",
        headers={
            "Content-Disposition": f'attachment; filename="{filename}"',
        },
    )


@router.get(
    "/verify",
    response_model=QRVerificationProbeResponse,
    summary="Cryptographic signature verification probe",
    status_code=status.HTTP_200_OK,
)
async def verify_qr_signature(
    table_id: Annotated[uuid.UUID, Query()],
    branch_id: Annotated[uuid.UUID, Query()],
    ts: Annotated[int, Query()],
    sig: Annotated[str, Query()],
    db: Annotated[AsyncSession, Depends(get_async_db)],
) -> QRVerificationProbeResponse:
    """Public verification probe validating table cryptographic signature and active DB state."""
    # 1. Verify HMAC-SHA256 signature
    is_valid = QRGeneratorService.verify_signed_url(
        table_id=table_id,
        branch_id=branch_id,
        timestamp=ts,
        signature=sig,
    )
    if not is_valid:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Invalid or tampered QR signature.",
        )

    # 2. Confirm table existence and active state
    stmt = select(Table).where(
        Table.id == table_id,
        Table.branch_id == branch_id,
        Table.is_active.is_(True),
    )
    result = await db.execute(stmt)
    table = result.scalar_one_or_none()

    if not table:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Table not found or inactive within specified branch.",
        )

    return QRVerificationProbeResponse(
        valid=True,
        table_id=table.id,
        branch_id=branch_id,
        table_number=table.table_number,
        is_active=table.is_active,
        timestamp=ts,
    )
