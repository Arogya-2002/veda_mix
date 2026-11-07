from collections.abc import AsyncIterator

from sqlalchemy import create_engine
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.orm import DeclarativeBase, sessionmaker

from veda.config.configuration import get_settings

_settings = get_settings()


class Base(DeclarativeBase):
    """All SQLAlchemy models inherit from this; Alembic reads Base.metadata."""


# API: async engine. Postgres defaults to READ COMMITTED; quota rows use explicit FOR UPDATE.
# ponytail: 5+5 per process to stay under Supabase's connection cap; raise with a bigger plan.
engine = create_async_engine(
    _settings.database_url, pool_size=5, max_overflow=5, pool_pre_ping=True
)
SessionLocal = async_sessionmaker(engine, expire_on_commit=False)

# Celery workers: sync engine, same models. Created lazily so the API never opens it.
_sync_engine = None


def sync_session() -> sessionmaker:
    global _sync_engine
    if _sync_engine is None:
        _sync_engine = create_engine(_settings.sync_database_url, pool_size=2, pool_pre_ping=True)
    return sessionmaker(_sync_engine, expire_on_commit=False)


async def get_db() -> AsyncIterator[AsyncSession]:
    """FastAPI dependency. Services own transactions with `async with db.begin()`."""
    async with SessionLocal() as session:
        yield session
