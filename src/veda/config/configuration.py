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
    gallery_base_url: str = "http://localhost:5173/g"  # QR codes point at {this}/{slug}
    razorpay_key_id: str = ""  # rzp_test_... in dev; public, sent to the React checkout
    razorpay_key_secret: str = ""
    razorpay_webhook_secret: str = ""  # set when creating the webhook in Razorpay's dashboard
    s3_bucket: str = ""
    s3_region: str = "ap-southeast-1"
    s3_endpoint_url: str = ""  # empty = real AWS; tests point this at a local fake
    # Empty = boto3's default chain (an IAM role on AWS). Locally: a bucket-scoped IAM user.
    aws_access_key_id: str = ""
    aws_secret_access_key: str = ""

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
