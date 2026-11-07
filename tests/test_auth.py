import re
import time
import uuid

from cryptography.hazmat.primitives.asymmetric import ec
from sqlalchemy import text

from tests.conftest import PROFILE, make_token


def bearer(token):
    return {"Authorization": f"Bearer {token}"}


def code(response):
    return response.json()["error"]["code"]


# --- token verification -----------------------------------------------------


def test_missing_token(client):
    assert code(client.get("/me")) == "UNAUTHENTICATED"


def test_rejects_bad_tokens(client, db):
    uid = uuid.uuid4()
    other_key = ec.generate_private_key(ec.SECP256R1())
    bad = {
        "wrong signature": make_token(uid, _key=other_key),
        "expired": make_token(uid, exp=int(time.time()) - 10),
        "wrong audience": make_token(uid, aud="anon"),
        "wrong issuer": make_token(uid, iss="https://evil.supabase.co/auth/v1"),
        "sub not a uuid": make_token(uid, sub="not-a-uuid"),
        "garbage": "abc.def.ghi",
    }
    for name, token in bad.items():
        r = client.get("/me", headers=bearer(token))
        assert r.status_code == 401 and code(r) == "UNAUTHENTICATED", name


def test_rejects_hs256_token(client, db):
    # Classic attack: sign with HS256 using the public key as the "secret".
    import jwt

    token = jwt.encode(
        {
            "sub": str(uuid.uuid4()),
            "aud": "authenticated",
            "iss": "https://test.supabase.co/auth/v1",
            "exp": int(time.time()) + 60,
        },
        "any-secret",
        algorithm="HS256",
    )
    assert client.get("/me", headers=bearer(token)).status_code == 401


# --- registration -------------------------------------------------------------


def test_register_then_me(client, new_user):
    _, headers = new_user("Pat@Example.com")
    assert code(client.get("/me", headers=headers)) == "NOT_REGISTERED"

    r = client.post("/auth/register", json=PROFILE, headers=headers)
    assert r.status_code == 201, r.text
    body = r.json()
    assert re.fullmatch(r"VP-[2-9A-HJ-NP-Z]{6}", body["public_id"])
    assert body["status"] == "PENDING_PAYMENT"
    assert body["email"] == "pat@example.com"  # lowercased

    assert client.get("/me", headers=headers).json() == body


def test_register_twice(client, new_user):
    _, headers = new_user()
    client.post("/auth/register", json=PROFILE, headers=headers)
    r = client.post("/auth/register", json=PROFILE, headers=headers)
    assert r.status_code == 409 and code(r) == "ALREADY_REGISTERED"


def test_phone_taken(client, new_user):
    _, first = new_user("a@example.com")
    _, second = new_user("b@example.com")
    client.post("/auth/register", json=PROFILE, headers=first)
    r = client.post("/auth/register", json=PROFILE, headers=second)
    assert r.status_code == 409 and code(r) == "PHONE_TAKEN"


def test_register_validates_phone(client, new_user):
    _, headers = new_user()
    r = client.post("/auth/register", json=PROFILE | {"phone": "9876543210"}, headers=headers)
    assert r.status_code == 422 and code(r) == "VALIDATION_ERROR"


def test_suspended_photographer_blocked(client, new_user, db):
    uid, headers = new_user()
    client.post("/auth/register", json=PROFILE, headers=headers)
    with db() as s, s.begin():
        s.execute(text("UPDATE photographers SET status='SUSPENDED' WHERE id=:id"), {"id": uid})
    r = client.get("/me", headers=headers)
    assert r.status_code == 403 and code(r) == "ACCOUNT_DISABLED"


# --- admin ----------------------------------------------------------------------


def test_admin_requires_admin_row_and_mfa(client, new_user, db):
    uid, aal1 = new_user("admin@example.com")
    assert code(client.get("/admin/me", headers=aal1)) == "FORBIDDEN"  # not an admin

    with db() as s, s.begin():
        s.execute(text("INSERT INTO admins (id) VALUES (:id)"), {"id": uid})
    assert code(client.get("/admin/me", headers=aal1)) == "MFA_REQUIRED"  # admin, no TOTP

    aal2 = bearer(make_token(uid, "admin@example.com", aal="aal2"))
    r = client.get("/admin/me", headers=aal2)
    assert r.status_code == 200 and r.json()["email"] == "admin@example.com"
