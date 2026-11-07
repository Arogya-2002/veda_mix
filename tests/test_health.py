from fastapi.testclient import TestClient

from app import app
from veda.exception import VedaException

client = TestClient(app, raise_server_exceptions=False)


@app.get("/_test/boom")
async def _boom():
    raise VedaException("QUOTA_EXCEEDED", "Over quota", status=413, details={"scope": "pool"})


def test_live():
    r = client.get("/health/live")
    assert r.status_code == 200 and r.json() == {"status": "ok"}
    assert r.headers["x-request-id"]


def test_error_shape():
    r = client.get("/_test/boom")
    assert r.status_code == 413
    assert r.json() == {
        "error": {"code": "QUOTA_EXCEEDED", "message": "Over quota", "details": {"scope": "pool"}}
    }


def test_custom_validator_error_is_json(client, active):
    """A field_validator's ValueError used to crash the 422 handler with a 500."""
    _, headers = active()
    eid = client.post("/events", json={"name": "x", "event_date": "2026-12-12"}, headers=headers)
    sid = client.post(f"/events/{eid.json()['id']}/upload-sessions", json={}, headers=headers)
    bad = {"filename": "a", "size_bytes": 1, "mime_type": "application/pdf", "sha256": "0" * 64}
    r = client.post(
        f"/upload-sessions/{sid.json()['id']}/files", json={"files": [bad]}, headers=headers
    )
    assert r.status_code == 422 and r.json()["error"]["code"] == "VALIDATION_ERROR"
