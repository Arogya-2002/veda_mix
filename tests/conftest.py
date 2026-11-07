import os

# Before any veda import: settings are read once. Env vars beat .env, so tests
# always use the local test DB and fake secrets, never the real values in .env.
os.environ["DATABASE_URL"] = os.environ.get(
    "TEST_DATABASE_URL", "postgresql+asyncpg://veda:veda@localhost:5432/veda_test"
)
os.environ["SUPABASE_URL"] = "https://test.supabase.co"
os.environ["RAZORPAY_KEY_ID"] = "rzp_test_key"
os.environ["RAZORPAY_KEY_SECRET"] = "test_key_secret"
os.environ["RAZORPAY_WEBHOOK_SECRET"] = "test_webhook_secret"

import hashlib
import hmac
import time
import uuid

import jwt
import pytest
from alembic import command
from alembic.config import Config
from cryptography.hazmat.primitives.asymmetric import ec
from fastapi.testclient import TestClient
from sqlalchemy import text

from veda.constants import ROOT_DIR
from veda.db import sync_session
from veda.integrations.payments import RazorpayGateway, get_payment_gateway

SIGNING_KEY = ec.generate_private_key(ec.SECP256R1())  # stands in for Supabase's key
KEEP_TABLES = {"alembic_version", "plans", "storage_pool"}  # migration-seeded rows survive
POOL_CAPACITY = 100 * 1024**3

PROFILE = {"full_name": "Pat Kumar", "studio_name": "Pat Studio", "phone": "+919876543210"}


def make_token(user_id: uuid.UUID, email: str | None = "pat@example.com", **overrides) -> str:
    """A token shaped like Supabase's; override any claim (or `_key`) to test rejection."""
    key = overrides.pop("_key", SIGNING_KEY)
    claims = {
        "sub": str(user_id),
        "email": email,
        "aal": "aal1",
        "role": "authenticated",
        "aud": "authenticated",
        "iss": "https://test.supabase.co/auth/v1",
        "exp": int(time.time()) + 300,
    } | overrides
    return jwt.encode(claims, key, algorithm="ES256")


def sign(secret: str, message: bytes) -> str:
    """What Razorpay does: HMAC-SHA256, hex."""
    return hmac.new(secret.encode(), message, hashlib.sha256).hexdigest()


class FakeRazorpay(RazorpayGateway):
    """Real signature checks; only the network call to create an order is faked."""

    def __init__(self):
        super().__init__("rzp_test_key", "test_key_secret", "test_webhook_secret")
        self.orders: list[dict] = []

    async def create_order(self, amount_paise: int, currency: str, receipt: str) -> str:
        self.orders.append({"amount": amount_paise, "currency": currency, "receipt": receipt})
        return f"order_{len(self.orders)}_{uuid.uuid4().hex[:6]}"


@pytest.fixture(scope="session")
def migrated():
    command.upgrade(Config(str(ROOT_DIR / "alembic.ini")), "head")


@pytest.fixture
def db(migrated):
    """Every table emptied before each test, except seeded ones (pool reset to 100 GB)."""
    with sync_session() as s, s.begin():
        tables = s.execute(
            text("SELECT tablename FROM pg_tables WHERE schemaname = 'public'")
        ).scalars()
        wipe = ", ".join(t for t in tables if t not in KEEP_TABLES)
        s.execute(text(f"TRUNCATE {wipe}, auth.users"))
        s.execute(text("UPDATE storage_pool SET capacity_bytes = :c"), {"c": POOL_CAPACITY})
    return sync_session


@pytest.fixture(scope="session")
def app():
    from app import app

    return app


@pytest.fixture(scope="session")
def client(app):
    # One TestClient for the session = one event loop, which pooled async DB connections need.
    with TestClient(app) as c:
        yield c


@pytest.fixture
def gateway(app):
    fake = FakeRazorpay()
    app.dependency_overrides[get_payment_gateway] = lambda: fake
    yield fake
    app.dependency_overrides.pop(get_payment_gateway)


@pytest.fixture(autouse=True)
def _fake_supabase_keys(monkeypatch):
    monkeypatch.setattr("veda.api.deps._signing_key", lambda token: SIGNING_KEY.public_key())


@pytest.fixture
def new_user(db):
    """Create a Supabase-style user row; returns (id, auth header)."""

    def _make(email="pat@example.com", **claims):
        user_id = uuid.uuid4()
        with sync_session() as s, s.begin():
            s.execute(
                text("INSERT INTO auth.users (id, email) VALUES (:id, :email)"),
                {"id": user_id, "email": email},
            )
        return user_id, {"Authorization": f"Bearer {make_token(user_id, email, **claims)}"}

    return _make


@pytest.fixture
def photographer(client, new_user):
    """A registered photographer who hasn't paid: (id, auth header)."""

    def _make(email="pat@example.com", phone="+919876543210"):
        user_id, headers = new_user(email)
        r = client.post("/auth/register", json=PROFILE | {"phone": phone}, headers=headers)
        assert r.status_code == 201, r.text
        return user_id, headers

    return _make
