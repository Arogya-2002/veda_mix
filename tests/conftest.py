import os
import socket


def _free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


S3_PORT = _free_port()
# Before any veda import: settings are read once. Env vars beat .env, so tests
# always use the local test DB and fake secrets, never the real values in .env.
os.environ["DATABASE_URL"] = os.environ.get(
    "TEST_DATABASE_URL", "postgresql+asyncpg://veda:veda@localhost:5432/veda_test"
)
os.environ["SUPABASE_URL"] = "https://test.supabase.co"
os.environ["ENV"] = "test"  # plain (non-Secure) cookies: the test client talks http
# Rate limits and live updates need Redis (docker compose up redis); db 15 keeps them apart.
os.environ["REDIS_URL"] = os.environ.get("TEST_REDIS_URL", "redis://localhost:6379/15")
os.environ["RAZORPAY_KEY_ID"] = "rzp_test_key"
os.environ["RAZORPAY_KEY_SECRET"] = "test_key_secret"
os.environ["RAZORPAY_WEBHOOK_SECRET"] = "test_webhook_secret"
os.environ["S3_ENDPOINT_URL"] = f"http://127.0.0.1:{S3_PORT}"  # moto, in-process
os.environ["S3_BUCKET"] = "veda-test"
os.environ["S3_REGION"] = "ap-southeast-1"  # must match the bucket the s3 fixture creates
os.environ["GALLERY_BASE_URL"] = "http://localhost:5173/g"
os.environ["AWS_ACCESS_KEY_ID"] = "test"
os.environ["AWS_SECRET_ACCESS_KEY"] = "test"

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
from moto.server import ThreadedMotoServer
from sqlalchemy import text

from veda.constants import ROOT_DIR
from veda.db import sync_session
from veda.integrations import storage
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
def s3():
    """A fake S3 on localhost. Unlike real S3 it doesn't enforce signed length/checksum."""
    server = ThreadedMotoServer(ip_address="127.0.0.1", port=S3_PORT, verbose=False)
    server.start()
    storage.client().create_bucket(
        Bucket="veda-test", CreateBucketConfiguration={"LocationConstraint": "ap-southeast-1"}
    )
    yield storage.client()
    server.stop()


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


@pytest.fixture
def active(photographer):
    """A paid photographer with a storage quota: (id, auth header)."""

    def _make(email="pat@example.com", phone="+919876543210", quota_bytes=2 * 1024**3):
        user_id, headers = photographer(email, phone)
        with sync_session() as s, s.begin():
            s.execute(
                text("UPDATE photographers SET status='ACTIVE' WHERE id=:id"), {"id": user_id}
            )
            s.execute(
                text("INSERT INTO storage_quotas (photographer_id, quota_bytes) VALUES (:id, :q)"),
                {"id": user_id, "q": quota_bytes},
            )
        return user_id, headers

    return _make


@pytest.fixture(autouse=True)
def enqueued(monkeypatch):
    """Photo ids the API queued for processing. Nothing reaches Redis in tests; run the
    work explicitly with process_photo.apply(args=[photo_id])."""
    calls: list[int] = []
    monkeypatch.setattr("veda.api.uploads.enqueue_photo_processing", calls.append)
    return calls
