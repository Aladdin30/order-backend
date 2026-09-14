"""Cryptographic QR Signature Engine: Stateless HMAC-SHA256 signing and verification."""

from __future__ import annotations

import base64
import hashlib
import hmac
import struct
import uuid
from typing import ClassVar

from app.core.config import settings
from app.schemas.qr import QRTokenPayload


class InvalidTokenError(Exception):
    """Raised when the QR token format, base64 encoding, or binary payload structure is invalid."""


class UnsupportedKeyVersionError(InvalidTokenError):
    """Raised when the QR token references an unsupported or revoked key version."""


class TokenTamperedError(InvalidTokenError):
    """Raised when HMAC-SHA256 signature verification fails due to tampering."""


class QRSignatureEngine:
    """High-security, tamper-proof Cryptographic QR Signature Engine for physical dining tables.

    Encodes tenant, branch, table identifiers and key version into a compact binary representation,
    serialized with URL-safe Base64 (RFC 4648 without padding), and authenticated with HMAC-SHA256.
    """

    # Versioned key registry for zero-downtime key rotation
    _keys: ClassVar[dict[int, str]] = {}

    @classmethod
    def _initialize_default_keys(cls) -> None:
        """Ensure active key registry is populated with configured default secrets."""
        if not cls._keys:
            default_secret = settings.QR_SECRET_KEY or settings.SECRET_KEY
            cls._keys[settings.QR_KEY_VERSION] = default_secret

    @classmethod
    def register_key(cls, version: int, secret_key: str) -> None:
        """Register or rotate a cryptographic signing key for a specific version."""
        cls._keys[version] = secret_key

    @classmethod
    def revoke_key(cls, version: int) -> None:
        """Revoke a specific key version to immediately invalidate tokens signed with it."""
        cls._keys.pop(version, None)

    @classmethod
    def get_key(cls, version: int) -> str:
        """Retrieve the secret key for the requested key version."""
        cls._initialize_default_keys()
        if version not in cls._keys:
            raise UnsupportedKeyVersionError(
                f"Unsupported or revoked key version: {version}. Active versions: {sorted(cls._keys.keys())}"
            )
        return cls._keys[version]

    @classmethod
    def sign_table_token(
        cls,
        tenant_id: uuid.UUID,
        branch_id: uuid.UUID,
        table_id: uuid.UUID,
        key_version: int = 1,
    ) -> str:
        """Generate a compact, URL-safe, tamper-proof physical table QR token.

        Format: <compact_urlsafe_payload>.<signature>
        """
        # Retrieve signing key for version
        secret_key = cls.get_key(key_version)

        # Pack payload into 52-byte binary: 3x UUID (16 bytes each) + 32-bit uint (4 bytes)
        payload_bytes = tenant_id.bytes + branch_id.bytes + table_id.bytes + struct.pack(">I", key_version)

        # Encode with URL-Safe Base64 without padding (RFC 4648)
        payload_b64 = base64.urlsafe_b64encode(payload_bytes).decode("ascii").rstrip("=")

        # Compute HMAC-SHA256 over URL-safe payload string bytes
        sig = hmac.new(secret_key.encode("utf-8"), payload_b64.encode("ascii"), hashlib.sha256).digest()
        sig_b64 = base64.urlsafe_b64encode(sig).decode("ascii").rstrip("=")

        return f"{payload_b64}.{sig_b64}"

    @classmethod
    def decode_and_verify_signature(cls, token: str) -> QRTokenPayload:
        """Verify token integrity via constant-time HMAC comparison and unpack into QRTokenPayload.

        Raises:
            InvalidTokenError: If token structure or base64 data is malformed.
            UnsupportedKeyVersionError: If token's key_version is not registered or revoked.
            TokenTamperedError: If HMAC signature comparison fails.
        """
        if not isinstance(token, str) or not token.strip():
            raise InvalidTokenError("Token cannot be empty or non-string.")

        parts = token.strip().split(".")
        if len(parts) != 2 or not parts[0] or not parts[1]:
            raise InvalidTokenError("Malformed QR token: expected '<payload>.<signature>' format.")

        payload_b64, sig_b64 = parts[0], parts[1]

        # Decode URL-safe Base64 payload with padding restored
        pad = len(payload_b64) % 4
        padded_payload = payload_b64 + ("=" * (4 - pad) if pad else "")
        try:
            raw_payload = base64.urlsafe_b64decode(padded_payload)
        except Exception as e:
            raise InvalidTokenError(f"Failed to decode base64 payload: {e}") from e

        # Unpack binary representation (52 bytes: 3 UUIDs + 4-byte int, or 50 bytes: 3 UUIDs + 2-byte short)
        if len(raw_payload) == 52:
            tenant_id = uuid.UUID(bytes=raw_payload[0:16])
            branch_id = uuid.UUID(bytes=raw_payload[16:32])
            table_id = uuid.UUID(bytes=raw_payload[32:48])
            key_version = struct.unpack(">I", raw_payload[48:52])[0]
        elif len(raw_payload) == 50:
            tenant_id = uuid.UUID(bytes=raw_payload[0:16])
            branch_id = uuid.UUID(bytes=raw_payload[16:32])
            table_id = uuid.UUID(bytes=raw_payload[32:48])
            key_version = struct.unpack(">H", raw_payload[48:50])[0]
        else:
            raise InvalidTokenError(
                f"Invalid payload length: expected 52 bytes binary payload, got {len(raw_payload)} bytes."
            )

        # Retrieve matching secret key for key_version
        secret_key = cls.get_key(key_version)

        # Compute expected HMAC signature
        expected_sig = hmac.new(
            secret_key.encode("utf-8"),
            payload_b64.encode("ascii"),
            hashlib.sha256,
        ).digest()
        expected_sig_b64 = base64.urlsafe_b64encode(expected_sig).decode("ascii").rstrip("=")

        # Constant-time comparison to prevent timing attacks
        if not hmac.compare_digest(expected_sig_b64, sig_b64):
            # Fallback: check if signature was computed directly over raw binary payload
            expected_sig_raw = hmac.new(
                secret_key.encode("utf-8"),
                raw_payload,
                hashlib.sha256,
            ).digest()
            expected_sig_raw_b64 = base64.urlsafe_b64encode(expected_sig_raw).decode("ascii").rstrip("=")
            if not hmac.compare_digest(expected_sig_raw_b64, sig_b64):
                raise TokenTamperedError(
                    "Cryptographic signature verification failed: token signature is invalid or payload has been tampered with."
                )

        return QRTokenPayload(
            tenant_id=tenant_id,
            branch_id=branch_id,
            table_id=table_id,
            key_version=key_version,
        )
