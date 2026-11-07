from functools import lru_cache
from pathlib import Path

import yaml
from pydantic import field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

from veda.constants import CONFIG_FILE_PATH, PARAMS_FILE_PATH, SCHEMA_FILE_PATH


class Settings(BaseSettings):
    """Secrets and per-environment values, from env vars or .env."""

    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    env: str = "dev"
    # Prod: Supabase via Supavisor *session* mode (port 5432) or the direct connection.
    # Not transaction mode (6543): it breaks asyncpg's prepared statements.
    database_url: str = "postgresql+asyncpg://veda:veda@localhost:5432/veda"
    redis_url: str = "redis://localhost:6379/0"
    supabase_url: str = ""

    @field_validator("database_url")
    @classmethod
    def _use_asyncpg(cls, v: str) -> str:
        # Accept the plain URL Supabase's dashboard gives (postgresql:// or postgres://).
        for prefix in ("postgresql://", "postgres://"):
            if v.startswith(prefix):
                return "postgresql+asyncpg://" + v.removeprefix(prefix)
        return v

    @property
    def sync_database_url(self) -> str:
        # Celery workers and Alembic use the sync driver against the same DB.
        return self.database_url.replace("+asyncpg", "+psycopg")


@lru_cache
def get_settings() -> Settings:
    return Settings()


def _read_yaml(path: Path) -> dict:
    with path.open(encoding="utf-8") as f:
        return yaml.safe_load(f) or {}


@lru_cache
def get_config() -> dict:
    return _read_yaml(CONFIG_FILE_PATH)


@lru_cache
def get_params() -> dict:
    return _read_yaml(PARAMS_FILE_PATH)


@lru_cache
def get_schema() -> dict:
    return _read_yaml(SCHEMA_FILE_PATH)
