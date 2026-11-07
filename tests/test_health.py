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
