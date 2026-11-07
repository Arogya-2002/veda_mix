import io

import segno
from sqlalchemy import text

from veda.utils.common import verify_pin

EVENT = {"name": "Asha & Ravi Wedding", "event_type": "wedding", "event_date": "2026-12-12"}


def code(r):
    return r.json()["error"]["code"]


def create(client, headers, **fields):
    r = client.post("/events", json=EVENT | fields, headers=headers)
    assert r.status_code == 201, r.text
    return r.json()


def audit_rows(db, event_id):
    with db() as s:
        return s.execute(
            text(
                "SELECT action, before_state, after_state FROM audit_logs"
                " WHERE entity_type='event' AND entity_id=:id ORDER BY id"
            ),
            {"id": str(event_id)},
        ).all()


# --- create, read, list ------------------------------------------------------------


def test_create_and_read(client, active, db):
    _, headers = active()
    event = create(client, headers)
    assert event["status"] == "UPCOMING" and event["visibility"] == "PUBLIC"
    assert len(event["public_slug"]) == 10
    assert event["gallery_url"] == f"http://localhost:5173/g/{event['public_slug']}"
    assert event["pin_set"] is False and "access_pin_hash" not in event

    assert client.get(f"/events/{event['id']}", headers=headers).json() == event
    assert [e["id"] for e in client.get("/events", headers=headers).json()] == [event["id"]]
    assert [a for a, *_ in audit_rows(db, event["id"])] == ["event.create"]


def test_list_filters_by_status(client, active):
    _, headers = active()
    live = create(client, headers)
    create(client, headers)
    client.post(f"/events/{live['id']}/go-live", headers=headers)
    r = client.get("/events?status=LIVE", headers=headers)
    assert [e["id"] for e in r.json()] == [live["id"]]


def test_unpaid_photographer_cannot_create(client, photographer):
    _, headers = photographer()
    r = client.post("/events", json=EVENT, headers=headers)
    assert r.status_code == 403 and code(r) == "SUBSCRIPTION_REQUIRED"


def test_expired_photographer_is_read_only(client, active, db):
    uid, headers = active()
    event = create(client, headers)
    with db() as s, s.begin():
        s.execute(text("UPDATE photographers SET status='EXPIRED' WHERE id=:id"), {"id": uid})

    assert client.get(f"/events/{event['id']}", headers=headers).status_code == 200
    for r in (
        client.post("/events", json=EVENT, headers=headers),
        client.patch(f"/events/{event['id']}", json={"name": "x"}, headers=headers),
        client.post(f"/events/{event['id']}/go-live", headers=headers),
        client.post(f"/events/{event['id']}/qr/regenerate", headers=headers),
    ):
        assert r.status_code == 403 and code(r) == "SUBSCRIPTION_REQUIRED"
    # Winding down still works.
    assert client.post(f"/events/{event['id']}/complete", headers=headers).status_code == 200
    assert client.delete(f"/events/{event['id']}", headers=headers).status_code == 204


# --- isolation (plan: A cannot see or change B's events) -----------------------------


def test_photographer_cannot_touch_another_photographers_event(client, active):
    _, alice = active("a@example.com", "+911111111111")
    _, bob = active("b@example.com", "+912222222222")
    eid = create(client, alice)["id"]

    for r in (
        client.get(f"/events/{eid}", headers=bob),
        client.patch(f"/events/{eid}", json={"name": "Mine now"}, headers=bob),
        client.delete(f"/events/{eid}", headers=bob),
        client.post(f"/events/{eid}/go-live", headers=bob),
        client.post(f"/events/{eid}/complete", headers=bob),
        client.get(f"/events/{eid}/qr", headers=bob),
        client.post(f"/events/{eid}/qr/regenerate", headers=bob),
    ):
        assert r.status_code == 404 and code(r) == "EVENT_NOT_FOUND"
    assert client.get("/events", headers=bob).json() == []
    after = client.get(f"/events/{eid}", headers=alice).json()
    assert after["name"] == EVENT["name"] and after["status"] == "UPCOMING"


# --- status changes ------------------------------------------------------------------


def test_status_lifecycle(client, active, db):
    _, headers = active()
    eid = create(client, headers)["id"]

    live = client.post(f"/events/{eid}/go-live", headers=headers).json()
    assert live["status"] == "LIVE" and live["went_live_at"]
    r = client.post(f"/events/{eid}/go-live", headers=headers)  # double-click
    assert r.status_code == 409 and code(r) == "INVALID_TRANSITION"
    r = client.post(f"/events/{eid}/archive", headers=headers)  # must complete first
    assert r.status_code == 409

    done = client.post(f"/events/{eid}/complete", headers=headers).json()
    assert done["status"] == "COMPLETED" and done["completed_at"]
    r = client.post(f"/events/{eid}/go-live", headers=headers)  # no going back
    assert r.status_code == 409
    assert client.post(f"/events/{eid}/archive", headers=headers).json()["status"] == "ARCHIVED"

    statuses = [(b, a) for action, b, a in audit_rows(db, eid) if action == "event.status"]
    assert statuses == [
        ({"status": "UPCOMING"}, {"status": "LIVE"}),
        ({"status": "LIVE"}, {"status": "COMPLETED"}),
        ({"status": "COMPLETED"}, {"status": "ARCHIVED"}),
    ]


# --- gallery settings ----------------------------------------------------------------


def test_pin_protected_gallery(client, active, db):
    _, headers = active()
    r = client.post("/events", json=EVENT | {"visibility": "PIN_PROTECTED"}, headers=headers)
    assert r.status_code == 400 and code(r) == "PIN_REQUIRED"

    event = create(client, headers, visibility="PIN_PROTECTED", pin="4821")
    assert event["pin_set"] is True
    with db() as s:
        stored = s.execute(text("SELECT access_pin_hash FROM events")).scalar()
    assert "4821" not in stored and verify_pin("4821", stored) and not verify_pin("1111", stored)

    # Change other settings without re-sending the PIN; it stays.
    r = client.patch(f"/events/{event['id']}", json={"allow_download": False}, headers=headers)
    assert r.json()["pin_set"] is True and r.json()["allow_download"] is False

    # Back to public forgets the PIN, and the audit log never holds the hash.
    r = client.patch(f"/events/{event['id']}", json={"visibility": "PUBLIC"}, headers=headers)
    assert r.json()["pin_set"] is False
    assert stored not in str(audit_rows(db, event["id"]))


def test_invalid_settings_rejected(client, active):
    _, headers = active()
    eid = create(client, headers)["id"]
    cases = [
        ({"pin": "12"}, 422),  # too short
        ({"name": None}, 422),  # required column can't be nulled
        ({"visibility": "SECRET"}, 422),
        ({"download_quality": "RAW"}, 422),
        ({"photographer_id": "00000000-0000-0000-0000-000000000000"}, 422),  # not settable
        ({"starts_at": "2026-12-12T18:00:00+05:30", "ends_at": "2026-12-12T10:00:00+05:30"}, 400),
    ]
    for body, status in cases:
        r = client.patch(f"/events/{eid}", json=body, headers=headers)
        assert r.status_code == status, (body, r.text)


# --- QR codes ------------------------------------------------------------------------


def test_qr_png_and_svg_encode_the_gallery_url(client, active):
    _, headers = active()
    event = create(client, headers)

    png = client.get(f"/events/{event['id']}/qr", headers=headers)
    assert png.headers["content-type"] == "image/png"
    assert f'filename="{event["public_slug"]}.png"' in png.headers["content-disposition"]
    expected = io.BytesIO()
    segno.make(event["gallery_url"], error="q").save(expected, kind="png", scale=10, border=4)
    assert png.content == expected.getvalue()

    svg = client.get(f"/events/{event['id']}/qr?format=svg", headers=headers)
    assert svg.headers["content-type"].startswith("image/svg+xml") and b"<svg" in svg.content


def test_regenerate_qr_replaces_the_link(client, active, db):
    _, headers = active()
    event = create(client, headers)
    new = client.post(f"/events/{event['id']}/qr/regenerate", headers=headers).json()
    assert new["public_slug"] != event["public_slug"]
    assert new["gallery_url"].endswith(new["public_slug"])
    action, before, after = audit_rows(db, event["id"])[-1]
    assert action == "event.new_link"
    assert (before, after) == (
        {"public_slug": event["public_slug"]},
        {"public_slug": new["public_slug"]},
    )


# --- delete --------------------------------------------------------------------------


def test_delete_is_soft(client, active, db):
    _, headers = active()
    eid = create(client, headers)["id"]
    assert client.delete(f"/events/{eid}", headers=headers).status_code == 204
    assert client.get(f"/events/{eid}", headers=headers).status_code == 404
    assert client.get("/events", headers=headers).json() == []
    with db() as s:
        assert s.execute(text("SELECT deleted_at IS NOT NULL FROM events")).scalar()
