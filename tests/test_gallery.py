import asyncio
import io
import os

import httpx
import pytest
import redis
from PIL import Image
from sqlalchemy import text

from tests.test_photo_processing import jpeg
from tests.test_uploads import meta, put
from veda.config.configuration import get_params, get_settings
from veda.db import sync_session
from veda.integrations import realtime
from veda.services import gallery
from veda.workers.tasks import process_photo

pytestmark = pytest.mark.usefixtures("s3")

EVENT = {"name": "Asha & Ravi Wedding", "event_date": "2026-12-12", "venue": "Bengaluru"}


@pytest.fixture(autouse=True)
def fresh_guest(client):
    """Each test starts as a new browser, with no rate-limit history."""
    client.cookies.clear()
    redis.Redis.from_url(get_settings().redis_url).flushdb()
    yield
    client.cookies.clear()


def code(r):
    return r.json()["error"]["code"]


def new_event(client, headers, **fields) -> dict:
    r = client.post("/events", json=EVENT | fields, headers=headers)
    assert r.status_code == 201, r.text
    return r.json()


def add_photo(client, headers, event_id, data=None, *, process=True) -> str:
    """Upload a photo to the event (and run the worker); returns its public_id."""
    data = data or jpeg(size=(600, 400), gps=False) + os.urandom(8)  # unique bytes, same image
    sid = client.post(f"/events/{event_id}/upload-sessions", json={}, headers=headers).json()["id"]
    decision = client.post(
        f"/upload-sessions/{sid}/files", json={"files": [meta(data)]}, headers=headers
    ).json()[0]
    put(decision["upload"], data)
    photo = client.post(f"/uploads/{decision['file_id']}/complete", headers=headers).json()
    if process:
        with sync_session() as s:
            photo_id = s.execute(
                text("SELECT id FROM photos WHERE public_id = :p"), {"p": photo["public_id"]}
            ).scalar_one()
        process_photo.apply(args=[photo_id])
    return photo["public_id"]


def sql(statement: str, **params):
    with sync_session() as s, s.begin():
        result = s.execute(text(statement), params)
        return result.all() if result.returns_rows else None


# --- viewing ------------------------------------------------------------------------------


def test_guest_sees_ready_photos_newest_first(client, active):
    _, headers = active()
    event = new_event(client, headers)
    first, second, third = (add_photo(client, headers, event["id"]) for _ in range(3))
    add_photo(client, headers, event["id"], process=False)  # still processing: not shown

    r = client.get(f"/public/galleries/{event['public_slug']}")  # no login
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["name"] == EVENT["name"] and body["studio_name"] == "Pat Studio"
    assert (body["locked"], body["photo_count"], body["status"]) == (False, 3, "UPCOMING")
    photos = body["page"]["photos"]
    assert [p["public_id"] for p in photos] == [third, second, first]
    assert body["page"]["next_cursor"] is None
    assert (photos[0]["width"], photos[0]["height"]) == (600, 400)

    thumb = httpx.get(photos[0]["thumb_url"])  # signed link straight to S3
    assert thumb.status_code == 200
    assert Image.open(io.BytesIO(thumb.content)).size == (400, 267)
    assert "veda_visitor" in r.cookies


def test_paging_walks_every_photo_once(client, active, monkeypatch):
    monkeypatch.setitem(get_params()["gallery"], "page_size", 2)
    _, headers = active()
    event = new_event(client, headers)
    uploaded = [add_photo(client, headers, event["id"]) for _ in range(5)]
    slug = event["public_slug"]

    seen, cursor = [], None
    for expected in (2, 2, 1):
        r = client.get(
            f"/public/galleries/{slug}/photos", params={"cursor": cursor} if cursor else {}
        )
        page = r.json()
        assert len(page["photos"]) == expected
        seen += [p["public_id"] for p in page["photos"]]
        cursor = page["next_cursor"]
    assert cursor is None
    assert seen == uploaded[::-1]

    r = client.get(f"/public/galleries/{slug}/photos", params={"cursor": "not-a-cursor"})
    assert r.status_code == 400 and code(r) == "INVALID_CURSOR"


def test_unique_visitors_and_page_views(client, active):
    _, headers = active()
    slug = new_event(client, headers)["public_slug"]
    client.get(f"/public/galleries/{slug}")
    client.get(f"/public/galleries/{slug}")  # same browser
    client.cookies.clear()
    client.get(f"/public/galleries/{slug}")  # another browser
    views = sql("SELECT page_views FROM gallery_visitors ORDER BY id")
    assert [v for (v,) in views] == [2, 1]


def test_galleries_closed_to_guests(client, active):
    user_id, headers = active()
    assert code(client.get("/public/galleries/nosuchslug")) == "GALLERY_NOT_FOUND"

    private = new_event(client, headers, visibility="PRIVATE")
    r = client.get(f"/public/galleries/{private['public_slug']}")
    assert r.status_code == 403 and code(r) == "GALLERY_UNAVAILABLE"

    event = new_event(client, headers)
    slug = event["public_slug"]
    client.post(f"/events/{event['id']}/complete", headers=headers)
    assert client.get(f"/public/galleries/{slug}").json()["status"] == "COMPLETED"  # still open
    client.post(f"/events/{event['id']}/archive", headers=headers)
    assert client.get(f"/public/galleries/{slug}").status_code == 403

    other = new_event(client, headers)
    sql("UPDATE photographers SET status = 'SUSPENDED' WHERE id = :id", id=user_id)
    assert client.get(f"/public/galleries/{other['public_slug']}").status_code == 403
    sql("UPDATE photographers SET status = 'ACTIVE' WHERE id = :id", id=user_id)
    client.delete(f"/events/{other['id']}", headers=headers)
    assert client.get(f"/public/galleries/{other['public_slug']}").status_code == 404


# --- PIN-protected galleries ---------------------------------------------------------------


def test_pin_unlocks_the_gallery(client, active):
    _, headers = active()
    event = new_event(client, headers, visibility="PIN_PROTECTED", pin="4821")
    photo = add_photo(client, headers, event["id"])
    slug = event["public_slug"]

    locked = client.get(f"/public/galleries/{slug}").json()
    assert (locked["locked"], locked["page"], locked["photo_count"]) == (True, None, None)
    assert locked["name"] == EVENT["name"]  # enough for the PIN screen
    for url in (
        f"/public/galleries/{slug}/photos",
        f"/public/galleries/{slug}/stream",
        f"/public/photos/{photo}/download",
    ):
        r = client.get(url, follow_redirects=False)
        assert r.status_code == 401 and code(r) == "PIN_REQUIRED", url

    r = client.post(f"/public/galleries/{slug}/unlock", json={"pin": "0000"})
    assert r.status_code == 401 and code(r) == "WRONG_PIN"
    r = client.post(f"/public/galleries/{slug}/unlock", json={"pin": "4821"})
    assert r.status_code == 204
    assert f"veda_gallery_{event['id']}" in r.cookies

    body = client.get(f"/public/galleries/{slug}").json()
    assert body["locked"] is False and body["page"]["photos"][0]["public_id"] == photo

    # A new PIN logs everyone out.
    client.patch(f"/events/{event['id']}", json={"pin": "9999"}, headers=headers)
    assert client.get(f"/public/galleries/{slug}").json()["locked"] is True


def test_pin_guessing_is_limited(client, active, monkeypatch):
    monkeypatch.setitem(get_params()["gallery"], "unlock_failures_per_ip", 3)
    _, headers = active()
    slug = new_event(client, headers, visibility="PIN_PROTECTED", pin="4821")["public_slug"]
    for guess in ("1111", "2222", "3333"):
        assert code(client.post(f"/public/galleries/{slug}/unlock", json={"pin": guess})) == (
            "WRONG_PIN"
        )
    r = client.post(f"/public/galleries/{slug}/unlock", json={"pin": "4821"})  # even the right one
    assert r.status_code == 429 and code(r) == "TOO_MANY_ATTEMPTS"


def test_unlock_needs_a_pin_event(client, active):
    _, headers = active()
    slug = new_event(client, headers)["public_slug"]
    r = client.post(f"/public/galleries/{slug}/unlock", json={"pin": "1234"})
    assert r.status_code == 409 and code(r) == "NO_PIN"


# --- downloads --------------------------------------------------------------------------


def test_downloads_follow_event_settings_and_are_logged(client, active):
    _, headers = active()
    event = new_event(client, headers)
    original = jpeg(size=(3000, 2000), gps=False)
    photo = add_photo(client, headers, event["id"], original)
    client.get(f"/public/galleries/{event['public_slug']}")  # guest gets a visitor cookie

    r = client.get(f"/public/photos/{photo}/download", follow_redirects=False)
    assert r.status_code == 302
    saved = httpx.get(r.headers["location"])
    assert Image.open(io.BytesIO(saved.content)).format == "WEBP"  # web copy by default
    assert "IMG_0001.webp" in saved.headers["content-disposition"]

    client.patch(f"/events/{event['id']}", json={"download_quality": "ORIGINAL"}, headers=headers)
    r = client.get(f"/public/photos/{photo}/download", follow_redirects=False)
    assert httpx.get(r.headers["location"]).content == original

    rows = sql("SELECT quality, visitor_id IS NOT NULL FROM photo_downloads ORDER BY id")
    assert rows == [("WEB", True), ("ORIGINAL", True)]

    client.patch(f"/events/{event['id']}", json={"allow_download": False}, headers=headers)
    r = client.get(f"/public/photos/{photo}/download", follow_redirects=False)
    assert r.status_code == 403 and code(r) == "DOWNLOAD_DISABLED"


def test_unready_or_unknown_photos_cannot_be_downloaded(client, active):
    _, headers = active()
    event = new_event(client, headers)
    pending = add_photo(client, headers, event["id"], process=False)
    for public_id in (pending, "nosuchphoto00000"):
        r = client.get(f"/public/photos/{public_id}/download", follow_redirects=False)
        assert r.status_code == 404 and code(r) == "PHOTO_NOT_FOUND"


# --- live updates -----------------------------------------------------------------------


def test_processed_photo_is_pushed_to_live_streams(client, active):
    _, headers = active()
    event = new_event(client, headers)
    public_id = add_photo(client, headers, event["id"], process=False)
    with sync_session() as s:
        photo_id = s.execute(
            text("SELECT id FROM photos WHERE public_id = :p"), {"p": public_id}
        ).scalar_one()

    async def listen():
        updates = realtime.photo_events(event["id"])
        try:
            assert await anext(updates) == {"type": "ready"}  # subscribed
            process_photo.apply(args=[photo_id])  # the worker publishes when READY
            return await asyncio.wait_for(anext(updates), timeout=5)
        finally:
            await updates.aclose()

    update = asyncio.run(listen())
    assert update["type"] == "photo" and update["photo"]["public_id"] == public_id
    shown = gallery.guest_photo(update["photo"])
    assert httpx.get(shown["web_url"]).status_code == 200


def test_stream_answers_503_when_live_updates_are_down(client, active, monkeypatch):
    async def broken(event_id):
        raise redis.ConnectionError("down")
        yield  # pragma: no cover

    monkeypatch.setattr(realtime, "photo_events", broken)
    _, headers = active()
    slug = new_event(client, headers)["public_slug"]
    r = client.get(f"/public/galleries/{slug}/stream")
    assert r.status_code == 503 and code(r) == "LIVE_UNAVAILABLE"
