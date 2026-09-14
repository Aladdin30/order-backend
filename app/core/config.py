"""Application configuration and security settings."""

import json

from typing import Any

from pydantic import field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Central configuration for authentication, JWT, database, and operational settings."""

    PROJECT_NAME: str = "Smart Restaurant Platform"
    API_V1_STR: str = "/api/v1"
    ENVIRONMENT: str = "development"

    # Security & JWT settings
    # 256-bit cryptographically secure default key for development; override via ENV in production
    _DEFAULT_SECRET_KEY: str = "7b23f8e5d3c1a49b6e0f2a8d5c7b1e4f9a3c6e8d0b2f5a7c9e1d4b6a8f0c2e4a"
    SECRET_KEY: str = _DEFAULT_SECRET_KEY
    ALGORITHM: str = "HS256"
    ACCESS_TOKEN_EXPIRE_MINUTES: int = 60

    # CORS
    BACKEND_CORS_ORIGINS: list[str] = ["*"]

    # Database URLs
    DATABASE_URL: str = "postgresql+asyncpg://postgres:postgres@localhost:5432/order_backend"
    SYNC_DATABASE_URL: str = "postgresql+psycopg2://postgres:postgres@localhost:5432/order_backend"

    # Audit Logging
    AUDIT_ENABLED: bool = True
    AUDIT_MUTATIONS_ONLY: bool = True

    # Cryptographic QR Signature Engine & Geofence
    QR_KEY_VERSION: int = 1
    QR_ACTIVE_KEY_VERSION: int | None = None
    QR_SECRET_KEY: str | None = None
    # JSON string mapping version (int) -> secret key (str), e.g. '{"1": "my-secret"}'
    QR_KEY_REGISTRY: str = ""
    # Explicitly revoked key versions (e.g. [1, 3] or "1,3")
    QR_REVOKED_KEYS: list[int] = []
    # When True, requests to /qr/verify MUST provide valid client GPS coordinates
    ENFORCE_GEOFENCE: bool = False

    # Rate Limiting & Proxy Trust Configuration
    RATE_LIMIT_ENABLED: bool = True
    # Storage backend for rate limiting: "memory" (single worker/tests) or "shared" (multi-worker SQLite/file)
    RATE_LIMIT_STORAGE_TYPE: str = "memory"
    # Shared SQLite database path for multi-worker synchronization
    RATE_LIMIT_STORAGE_PATH: str = "/tmp/order_backend_ratelimit.db"
    # Trusted reverse proxy IP addresses or CIDR networks (e.g. Nginx, ALB, Cloudflare)
    TRUSTED_PROXIES: list[str] = ["127.0.0.1", "::1"]
    RATE_LIMIT_MAX_TRACKED_KEYS: int = 10_000
    RATE_LIMIT_CLEANUP_INTERVAL_SECONDS: float = 30.0

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    @field_validator("TRUSTED_PROXIES", "BACKEND_CORS_ORIGINS", mode="before")
    @classmethod
    def _parse_comma_separated_list(cls, v: Any) -> Any:
        if isinstance(v, str) and not v.startswith("["):
            return [item.strip() for item in v.split(",") if item.strip()]
        return v

    @field_validator("QR_REVOKED_KEYS", mode="before")
    @classmethod
    def _parse_revoked_keys(cls, v: Any) -> Any:
        if isinstance(v, str):
            v = v.strip()
            if not v:
                return []
            if v.startswith("["):
                try:
                    return [int(x) for x in json.loads(v)]
                except (json.JSONDecodeError, ValueError):
                    pass
            return [int(x.strip()) for x in v.split(",") if x.strip()]
        if isinstance(v, (list, tuple, set)):
            return [int(x) for x in v]
        return v

    @model_validator(mode="after")
    def _validate_production_secrets(self) -> "Settings":
        """Block startup if SECRET_KEY is the insecure default in non-development environments."""
        if self.ENVIRONMENT.lower() not in ("development", "dev", "test", "testing") and self.SECRET_KEY == self._DEFAULT_SECRET_KEY:
            raise ValueError(
                "CRITICAL: SECRET_KEY must be explicitly set via environment variable "
                "in non-development environments. The default development key is not "
                "permitted for ENVIRONMENT='" + self.ENVIRONMENT + "'."
            )
        return self

    def get_qr_key_registry(self) -> dict[int, str]:
        """Parse QR_KEY_REGISTRY with internal caching to avoid redundant JSON parsing."""
        cached_dict = getattr(self, "_cached_registry_dict", None)
        cached_raw = getattr(self, "_cached_registry_raw", None)

        if cached_dict is not None and cached_raw == self.QR_KEY_REGISTRY:
            return dict(cached_dict)

        if not self.QR_KEY_REGISTRY:
            result: dict[int, str] = {}
        else:
            try:
                raw = json.loads(self.QR_KEY_REGISTRY)
                result = {int(k): str(v) for k, v in raw.items()}
            except (json.JSONDecodeError, ValueError):
                result = {}

        object.__setattr__(self, "_cached_registry_dict", result)
        object.__setattr__(self, "_cached_registry_raw", self.QR_KEY_REGISTRY)
        return dict(result)

    def get_revoked_key_versions(self) -> set[int]:
        """Return set of explicitly revoked key versions."""
        return set(self.QR_REVOKED_KEYS)

    def update_qr_key_registry(self, new_registry: dict[int, str] | str) -> None:
        """Safely update key registry at runtime and refresh cache."""
        if isinstance(new_registry, dict):
            self.QR_KEY_REGISTRY = json.dumps({str(k): v for k, v in new_registry.items()})
        else:
            self.QR_KEY_REGISTRY = str(new_registry)
        # Invalidate cache
        object.__setattr__(self, "_cached_registry_dict", None)
        object.__setattr__(self, "_cached_registry_raw", None)


settings = Settings()
