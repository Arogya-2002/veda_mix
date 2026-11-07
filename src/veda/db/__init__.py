from collections.abc import AsyncIterator

from sqlalchemy import MetaData, create_engine
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.orm import DeclarativeBase, Session

from veda.config.configuration import get_settings

_settings = get_settings()


class Base(DeclarativeBase):
    """All SQLAlchemy models inherit from this; Alembic reads Base.metadata."""

    # Predictable constraint names (e.g. uq_photographers_phone), so code can tell
    # which constraint a duplicate hit, and migrations name things consistently.
    metadata = MetaData(
        naming_convention={
            "pk": "pk_%(table_name)s",
            "uq": "uq_%(table_name)s_%(column_0_N_name)s",
            "ix": "ix_%(table_name)s_%(column_0_N_name)s",
            "ck": "ck_%(table_name)s_%(constraint_name)s",
            "fk": "fk_%(table_name)s_%(column_0_name)s_%(referred_table_name)s",
        }
    )


# API: async engine. Postgres defaults to READ COMMITTED; quota rows use explicit FOR UPDATE.
# ponytail: 5+5 per process to stay under Supabase's connection cap; raise with a bigger plan.
engine = create_async_engine(
    _settings.database_url, pool_size=5, max_overflow=5, pool_pre_ping=True
)
SessionLocal = async_sessionmaker(engine, expire_on_commit=False)

# Celery workers and the CLI: sync engine, same models. Created lazily so the API never opens it.
_sync_engine = None


def sync_session() -> Session:
    """Usage: `with sync_session() as s, s.begin(): ...`"""
    global _sync_engine
    if _sync_engine is None:
        _sync_engine = create_engine(_settings.sync_database_url, pool_size=2, pool_pre_ping=True)
    return Session(_sync_engine, expire_on_commit=False)


async def get_db() -> AsyncIterator[AsyncSession]:
    """FastAPI dependency: one session per request. Its transaction starts on the first
    query (often in an auth dependency); services never commit, the route commits once."""
    async with SessionLocal() as session:
        yield session
