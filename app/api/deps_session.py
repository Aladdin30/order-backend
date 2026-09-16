"""Guest session security dependencies for downstream route protection."""

from __future__ import annotations

import uuid

from fastapi import Depends, HTTPException, status
from fastapi.security import OAuth2PasswordBearer

from app.core.config import settings
from app.core.session_security import (
    ExpiredGuestSessionError,
    GuestSessionJWTError,
    InvalidGuestSessionError,
    decode_guest_session_jwt,
)
from app.schemas.session import GuestSessionContext

guest_oauth2_scheme = OAuth2PasswordBearer(
    tokenUrl=f"{settings.API_V1_STR}/sessions/verify-presence",
    auto_error=True,
)


async def get_current_guest_session(
    token: str = Depends(guest_oauth2_scheme),
) -> GuestSessionContext:
    """Validate Guest Session JWT and construct immutable GuestSessionContext.

    Raises:
        HTTPException(401): If token is expired, tampered, invalid, or missing required claims.
    """
    credentials_exception = HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail="Could not validate guest session credentials",
        headers={"WWW-Authenticate": "Bearer"},
    )

    try:
        payload = decode_guest_session_jwt(token)
        return GuestSessionContext(
            session_id=uuid.UUID(payload["sub"]),
            tenant_id=uuid.UUID(payload["tenant_id"]),
            branch_id=uuid.UUID(payload["branch_id"]),
            table_id=uuid.UUID(payload["table_id"]),
            table_number=str(payload["table_number"]),
            is_presence_verified=bool(payload["is_presence_verified"]),
        )
    except (GuestSessionJWTError, ValueError, KeyError) as exc:
        raise credentials_exception from exc


async def require_presence_verified(
    session: GuestSessionContext = Depends(get_current_guest_session),
) -> GuestSessionContext:
    """FastAPI dependency requiring that guest presence has been verified within geofence boundaries.

    Raises:
        HTTPException(403): If guest session does not have presence verified.
    """
    if not session.is_presence_verified:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="PRESENCE_VERIFICATION_REQUIRED",
        )
    return session


class RequirePresenceVerifiedDependency:
    """Callable class dependency matching RequirePresenceVerified."""

    async def __call__(
        self,
        session: GuestSessionContext = Depends(get_current_guest_session),
    ) -> GuestSessionContext:
        return await require_presence_verified(session)


RequirePresenceVerified = RequirePresenceVerifiedDependency()
