"""Application configuration and security settings."""

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Central configuration for authentication, JWT, database, and operational settings."""

    PROJECT_NAME: str = "Smart Restaurant Platform"
    API_V1_STR: str = "/api/v1"

    # Security & JWT settings
    # 256-bit cryptographically secure default key for development; override via ENV in production
    SECRET_KEY: str = "7b23f8e5d3c1a49b6e0f2a8d5c7b1e4f9a3c6e8d0b2f5a7c9e1d4b6a8f0c2e4a"
    ALGORITHM: str = "HS256"
    ACCESS_TOKEN_EXPIRE_MINUTES: int = 60

    # Database URLs
    DATABASE_URL: str = "postgresql+asyncpg://postgres:postgres@localhost:5432/order_backend"
    SYNC_DATABASE_URL: str = "postgresql+psycopg2://postgres:postgres@localhost:5432/order_backend"

    # Audit Logging
    AUDIT_ENABLED: bool = True
    AUDIT_MUTATIONS_ONLY: bool = True

    # Cryptographic QR Signature Engine
    QR_KEY_VERSION: int = 1
    QR_SECRET_KEY: str | None = None

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )


settings = Settings()
