"""Guest Session Security: Cryptographic JWT generation and validation for dining sessions."""

from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone
from typing import Any

import jwt

from app.core.config import settings


class GuestSessionJWTError(Exception):
    """Base exception for guest session token failures."""


class InvalidGuestSessionError(GuestSessionJWTError):
    """Raised when guest session JWT structure, signature, or claims are invalid."""


class ExpiredGuestSessionError(GuestSessionJWTError):
    """Raised when guest session JWT has expired."""


def create_guest_session_jwt(
    session_id: uuid.UUID,
    tenant_id: uuid.UUID,
    branch_id: uuid.UUID,
    table_id: uuid.UUID,
    table_number: str,
    is_presence_verified: bool,
    expires_delta: timedelta | None = None,
) -> str:
    """Issue a cryptographically signed, short-lived Guest Session JWT.

    Claims:
        sub: Unique session UUID string
        type: "guest_session"
        tenant_id: Tenant UUID string
        branch_id: Branch UUID string
        table_id: Table UUID string
        table_number: String identifier of table (e.g. "T-01")
        is_presence_verified: Boolean presence status
        iat: UTC issuance timestamp
        exp: UTC expiration timestamp
    """
    now = datetime.now(timezone.utc)
    if expires_delta is not None:
        expire = now + expires_delta
    else:
        expire = now + timedelta(minutes=settings.GUEST_SESSION_EXPIRE_MINUTES)

    payload: dict[str, Any] = {
        "sub": str(session_id),
        "type": "guest_session",
        "tenant_id": str(tenant_id),
        "branch_id": str(branch_id),
        "table_id": str(table_id),
        "table_number": str(table_number),
        "is_presence_verified": bool(is_presence_verified),
        "iat": int(now.timestamp()),
        "exp": int(expire.timestamp()),
    }

    return jwt.encode(payload, settings.SECRET_KEY, algorithm=settings.ALGORITHM)


def decode_guest_session_jwt(token: str) -> dict[str, Any]:
    """Decode and validate a Guest Session JWT.

    Raises:
        ExpiredGuestSessionError: If token expiration has passed.
        InvalidGuestSessionError: If token signature, claims, or type is invalid.
    """
    if not isinstance(token, str) or not token.strip():
        raise InvalidGuestSessionError("Token must be a non-empty string.")

    try:
        payload = jwt.decode(
            token.strip(),
            settings.SECRET_KEY,
            algorithms=[settings.ALGORITHM],
            options={"require": ["exp", "iat", "sub"]},
        )
    except jwt.ExpiredSignatureError as e:
        raise ExpiredGuestSessionError("Guest session token has expired.") from e
    except jwt.PyJWTError as e:
        raise InvalidGuestSessionError(f"Invalid guest session token: {e}") from e

    token_type = payload.get("type")
    if token_type != "guest_session":
        raise InvalidGuestSessionError(
            f"Invalid token type: expected 'guest_session', got '{token_type}'."
        )

    required_claims = ["tenant_id", "branch_id", "table_id", "table_number", "is_presence_verified"]
    missing = [c for c in required_claims if c not in payload]
    if missing:
        raise InvalidGuestSessionError(f"Missing required session claims: {missing}")

    return payload
