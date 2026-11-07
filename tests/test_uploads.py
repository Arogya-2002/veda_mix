import asyncio
import hashlib
import os
from datetime import UTC, datetime, timedelta

import httpx
import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from veda.config.configuration import get_settings
from veda.db.models import Photographer
from veda.services import uploads

pytestmark = pytest.mark.usefixtures("s3")

EVENT = {"name": "Asha & Ravi Wedding", "event_date": "2026-12-12"}


def code(r):
    return r.json()["error"]["code"]


def meta(data: bytes, name="IMG_0001.jpg", mime="image/jpeg") -> dict:
    return {
        "filename": name,
        "size_bytes": len(data),
        "mime_type": mime,
        "sha256": hashlib.sha256(data).hexdigest(),
    }


def put(upload: dict, data: bytes) -> httpx.Response:
    """What the browser does with the instructions: PUT straight to S3."""
    r = httpx.put(upload["url"], content=data, headers=upload["headers"])
    assert r.status_code == 200, r.text
    return r


def open_session(client, headers) -> tuple[int, int]:
    event_id = client.post("/events", json=EVENT, headers=headers).json()["id"]
    r = client.post(
        f"/events/{event_id}/upload-sessions",
        json={"source": "FOLDER_WATCH", "device_label": "Canon R6"},
        headers=headers,
    )
    assert r.status_code == 201, r.text
    return event_id, r.json()["id"]


def request(client, headers, session_id, *blobs):
    r = client.post(
        f"/upload-sessions/{session_id}/files",
        json={"files": [meta(b, f"IMG_{i:04}.jpg") for i, b in enumerate(blobs)]},
        headers=headers,
    )
    assert r.status_code == 200, r.text
    return r.json()


def storage_of(client, headers):
    return client.get("/me/storage", headers=headers).json()


def upload_one(client, headers, session_id, data):
    decision = request(client, headers, session_id, data)[0]
    put(decision["upload"], data)
    r = client.post(f"/uploads/{decision['file_id']}/complete", headers=headers)
    assert r.status_code == 200, r.text
    return decision["file_id"], r.json()


# --- the happy path -------------------------------------------------------------------


def test_upload_reserve_put_complete(client, active, s3):
    _, headers = active()
    event_id, sid = open_session(client, headers)
    data = os.urandom(1000)

    decision = request(client, headers, sid, data)[0]
    assert decision["decision"] == "UPLOAD"
    assert storage_of(client, headers)["reserved_bytes"] == 1000

    put(decision["upload"], data)
    r = client.post(f"/uploads/{decision['file_id']}/complete", headers=headers)
    assert r.status_code == 200, r.text
    photo = r.json()
    assert photo["status"] == "PROCESSING" and photo["event_id"] == event_id
    assert len(photo["public_id"]) == 16

    assert storage_of(client, headers) | {"quota_bytes": 0} == {
        "quota_bytes": 0,
        "used_bytes": 1000,
        "reserved_bytes": 0,
        "available_bytes": 2 * 1024**3 - 1000,
    }
    status = client.get(f"/upload-sessions/{sid}", headers=headers).json()
    assert status["files"]["COMPLETED"] == 1 and status["source"] == "FOLDER_WATCH"

    # Completing again (a retried request) gives the same photo, counts nothing twice.
    again = client.post(f"/uploads/{decision['file_id']}/complete", headers=headers).json()
    assert again["public_id"] == photo["public_id"]
    assert storage_of(client, headers)["used_bytes"] == 1000


def test_hundred_photos_quota_matches_bucket(client, active, s3, db):
    """Plan's 'done when': 100 photos straight to the bucket, quota numbers match."""
    _, headers = active()
    _, sid = open_session(client, headers)
    blobs = [os.urandom(500 + i) for i in range(100)]

    decisions = request(client, headers, sid, *blobs[:50]) + request(
        client, headers, sid, *blobs[50:]
    )
    for decision, data in zip(decisions, blobs, strict=True):
        put(decision["upload"], data)
        r = client.post(f"/uploads/{decision['file_id']}/complete", headers=headers)
        assert r.status_code == 200, r.text

    public_id = client.get("/me", headers=headers).json()["public_id"]
    listing = s3.list_objects_v2(Bucket="veda-test", Prefix=f"originals/{public_id}/")
    in_bucket = sum(o["Size"] for o in listing["Contents"])
    with db() as s:
        photo_bytes = s.execute(text("SELECT sum(size_bytes), count(*) FROM photos")).one()
    assert storage_of(client, headers)["used_bytes"] == photo_bytes[0] == in_bucket
    assert photo_bytes[1] == 100 and storage_of(client, headers)["reserved_bytes"] == 0


# --- duplicates and quota ---------------------------------------------------------------


def test_duplicates_rejected_within_an_event(client, active):
    _, headers = active()
    _, sid = open_session(client, headers)
    data = os.urandom(100)

    first, second = request(client, headers, sid, data, data)
    assert (first["decision"], second["decision"]) == ("UPLOAD", "DUPLICATE")
    assert request(client, headers, sid, data)[0]["decision"] == "DUPLICATE"  # still in flight

    put(first["upload"], data)
    client.post(f"/uploads/{first['file_id']}/complete", headers=headers)
    assert request(client, headers, sid, data)[0]["decision"] == "DUPLICATE"  # now a photo

    _, other_event_session = open_session(client, headers)
    assert request(client, headers, other_event_session, data)[0]["decision"] == "UPLOAD"


def test_quota_fills_in_order(client, active):
    _, headers = active(quota_bytes=100)
    _, sid = open_session(client, headers)
    decisions = request(client, headers, sid, os.urandom(60), os.urandom(60), os.urandom(30))
    assert [d["decision"] for d in decisions] == ["UPLOAD", "QUOTA_EXCEEDED", "UPLOAD"]
    assert decisions[1]["upload"] is None
    assert storage_of(client, headers)["reserved_bytes"] == 90


def test_parallel_reservations_never_exceed_quota(client, active, db):
    """Plan: 20 parallel reservations against a nearly full quota -> never over the limit."""
    uid, headers = active(quota_bytes=95)
    _, sid = open_session(client, headers)

    async def race():
        engine = create_async_engine(get_settings().database_url, pool_size=20)
        sessions = async_sessionmaker(engine, expire_on_commit=False)

        async def one(i):
            async with sessions() as s:
                photographer = await s.get(Photographer, uid)
                answer = await uploads.request_files(s, photographer, sid, [meta(bytes([i]) * 10)])
                await s.commit()
                return answer[0]["decision"]

        try:
            return await asyncio.gather(*(one(i) for i in range(20)))
        finally:
            await engine.dispose()

    decisions = asyncio.run(race())
    assert decisions.count("UPLOAD") == 9  # 9 x 10 bytes fit in 95
    assert storage_of(client, headers)["reserved_bytes"] == 90
    with db() as s:
        assert s.execute(text("SELECT count(*) FROM upload_files")).scalar() == 9


# --- completing, retrying, aborting -------------------------------------------------------


def test_complete_before_upload(client, active):
    _, headers = active()
    _, sid = open_session(client, headers)
    data = os.urandom(100)
    decision = request(client, headers, sid, data)[0]

    r = client.post(f"/uploads/{decision['file_id']}/complete", headers=headers)
    assert r.status_code == 409 and code(r) == "NOT_UPLOADED"
    put(decision["upload"], data)
    assert client.post(f"/uploads/{decision['file_id']}/complete", headers=headers).is_success


def test_mismatched_upload_fails_then_retry_succeeds(client, active):
    _, headers = active()
    _, sid = open_session(client, headers)
    data = os.urandom(100)
    decision = request(client, headers, sid, data)[0]
    fid = decision["file_id"]

    # Real S3 refuses this PUT outright (signed length); the fake accepts it, so
    # /complete's own check is what's tested here.
    put(decision["upload"], data[:50])
    r = client.post(f"/uploads/{fid}/complete", headers=headers)
    assert r.status_code == 422 and code(r) == "UPLOAD_MISMATCH"
    assert storage_of(client, headers)["reserved_bytes"] == 100  # still held for the retry

    retry = client.post(f"/uploads/{fid}/retry", headers=headers)
    assert retry.status_code == 200, retry.text
    put(retry.json(), data)
    assert client.post(f"/uploads/{fid}/complete", headers=headers).is_success
    assert storage_of(client, headers)["used_bytes"] == 100


def test_retry_limit(client, active):
    _, headers = active()
    _, sid = open_session(client, headers)
    fid = request(client, headers, sid, os.urandom(10))[0]["file_id"]
    assert client.post(f"/uploads/{fid}/retry", headers=headers).is_success  # attempt 2
    assert client.post(f"/uploads/{fid}/retry", headers=headers).is_success  # attempt 3
    r = client.post(f"/uploads/{fid}/retry", headers=headers)
    assert r.status_code == 409 and code(r) == "TOO_MANY_ATTEMPTS"


def test_abort_releases_space_and_object(client, active, s3):
    _, headers = active()
    _, sid = open_session(client, headers)
    data = os.urandom(100)
    decision = request(client, headers, sid, data)[0]
    put(decision["upload"], data)

    assert client.post(f"/uploads/{decision['file_id']}/abort", headers=headers).status_code == 204
    assert storage_of(client, headers)["reserved_bytes"] == 0
    key = decision["upload"]["url"].split("veda-test/")[1].split("?")[0]
    assert s3.list_objects_v2(Bucket="veda-test", Prefix=key).get("KeyCount") == 0
    r = client.post(f"/uploads/{decision['file_id']}/abort", headers=headers)
    assert r.status_code == 409 and code(r) == "UPLOAD_CLOSED"
    # The same file can be requested again.
    assert request(client, headers, sid, data)[0]["decision"] == "UPLOAD"


def test_expired_reservations_are_released(client, active, db):
    _, headers = active()
    _, sid = open_session(client, headers)
    data = os.urandom(100)
    decision = request(client, headers, sid, data)[0]
    put(decision["upload"], data)  # uploaded but never completed

    with db() as s, s.begin():
        assert uploads.release_expired(s, datetime.now(UTC)) == []  # not due yet
    with db() as s, s.begin():
        keys = uploads.release_expired(s, datetime.now(UTC) + timedelta(hours=2))
    assert len(keys) == 1 and keys[0].startswith("originals/VP-")
    assert storage_of(client, headers)["reserved_bytes"] == 0

    r = client.post(f"/uploads/{decision['file_id']}/complete", headers=headers)
    assert r.status_code == 409 and code(r) == "UPLOAD_CLOSED"


# --- rules ------------------------------------------------------------------------------


def test_uploads_blocked_once_event_completed(client, active):
    _, headers = active()
    event_id, sid = open_session(client, headers)
    client.post(f"/events/{event_id}/complete", headers=headers)

    r = client.post(f"/events/{event_id}/upload-sessions", json={}, headers=headers)
    assert r.status_code == 409 and code(r) == "EVENT_CLOSED"
    r = client.post(f"/upload-sessions/{sid}/files", json={"files": [meta(b"x")]}, headers=headers)
    assert r.status_code == 409 and code(r) == "EVENT_CLOSED"


def test_unpaid_photographer_cannot_upload(client, photographer, active):
    _, paid = active("a@example.com", "+911111111111")
    event_id = client.post("/events", json=EVENT, headers=paid).json()["id"]
    _, unpaid = photographer("b@example.com", "+912222222222")
    r = client.post(f"/events/{event_id}/upload-sessions", json={}, headers=unpaid)
    assert r.status_code == 403 and code(r) == "SUBSCRIPTION_REQUIRED"


def test_photographer_cannot_touch_another_photographers_uploads(client, active):
    _, alice = active("a@example.com", "+911111111111")
    _, bob = active("b@example.com", "+912222222222")
    event_id, sid = open_session(client, alice)
    fid = request(client, alice, sid, os.urandom(10))[0]["file_id"]

    for r in (
        client.post(f"/events/{event_id}/upload-sessions", json={}, headers=bob),
        client.get(f"/upload-sessions/{sid}", headers=bob),
        client.post(f"/upload-sessions/{sid}/files", json={"files": [meta(b"x")]}, headers=bob),
        client.post(f"/uploads/{fid}/complete", headers=bob),
        client.post(f"/uploads/{fid}/retry", headers=bob),
        client.post(f"/uploads/{fid}/abort", headers=bob),
    ):
        assert r.status_code == 404, r.text
    assert storage_of(client, alice)["reserved_bytes"] == 10


def test_file_validation(client, active):
    _, headers = active()
    _, sid = open_session(client, headers)
    good = meta(b"x")
    for bad in (
        good | {"mime_type": "application/pdf"},
        good | {"size_bytes": 50 * 1024 * 1024 + 1},
        good | {"sha256": "ABC"},
        good | {"size_bytes": 0},
    ):
        r = client.post(f"/upload-sessions/{sid}/files", json={"files": [bad]}, headers=headers)
        assert r.status_code == 422, bad
    r = client.post(f"/upload-sessions/{sid}/files", json={"files": [good] * 51}, headers=headers)
    assert r.status_code == 422
