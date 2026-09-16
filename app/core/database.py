"""Asynchronous database engine, sessionmaker, and dialect compilation hooks."""

from typing import AsyncGenerator

from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from sqlalchemy.ext.compiler import compiles

from app.core.config import settings

# SQLite dialect compilation hook to allow JSONB compilation during tests
@compiles(JSONB, "sqlite")
def _compile_jsonb_sqlite(type_, compiler, **kw) -> str:
    return "JSON"


def create_engine_instance(db_url: str | None = None) -> AsyncEngine:
    """Instantiate an AsyncEngine configured for high concurrency."""
    url = db_url or settings.DATABASE_URL

    # Pool settings only apply to connection-pooled drivers (not SQLite)
    pool_kwargs: dict = {}
    if not url.startswith("sqlite"):
        pool_kwargs = {
            "pool_pre_ping": True,
            "pool_size": 20,
            "max_overflow": 10,
            "pool_recycle": 1800,
        }

    return create_async_engine(
        url,
        echo=False,
        future=True,
        **pool_kwargs,
    )


async_engine = create_engine_instance()

async_session_factory = async_sessionmaker(
    bind=async_engine,
    class_=AsyncSession,
    autoflush=False,
    expire_on_commit=False,
)


async def get_async_session() -> AsyncGenerator[AsyncSession, None]:
    """Dependency helper providing an active AsyncSession."""
    async with async_session_factory() as session:
        yield session
