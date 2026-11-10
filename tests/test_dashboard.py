import os
from datetime import date, datetime

import pytest

from tests.test_gallery import EVENT, add_photo, fresh_guest, new_event, sql  # noqa: F401
from tests.test_payments import checkout_result, order
from tests.test_photo_processing import jpeg
from veda.config.configuration import local_tz

pytestmark = pytest.mark.usefixtures("s3", "fresh_guest")


def code(r):
    return r.json()["error"]["code"]


def pay(client, headers, payment_id="pay_dash"):
    """A real (fake-gateway) purchase: subscription, quota and ACTIVE status."""
    order_id = order(client, headers)["order_id"]
    r = client.post("/payments/verify", json=checkout_result(order_id, payment_id), headers=headers)
    assert r.status_code == 200, r.text


def test_summary_counts_everything_the_photographer_owns(client, photographer, gateway):
    _, headers = photographer()
    pay(client, headers)
    live = new_event(client, headers)
    client.post(f"/events/{live['id']}/go-live", headers=headers)
    new_event(client, headers, name="Reception")
    photo = add_photo(client, headers, live["id"])
    add_photo(client, headers, live["id"])
    add_photo(client, headers, live["id"], process=False)
    add_photo(client, headers, live["id"], os.urandom(3000))  # not an image: FAILED

    slug = live["public_slug"]
    client.get(f"/public/galleries/{slug}")
    client.get(f"/public/galleries/{slug}")
    client.cookies.clear()
    client.get(f"/public/galleries/{slug}")
    client.get(f"/public/photos/{photo}/download", follow_redirects=False)

    r = client.get("/dashboard/summary", headers=headers)
    assert r.status_code == 200, r.text
    s = r.json()
    assert s["events"] == {"UPCOMING": 1, "LIVE": 1, "COMPLETED": 0, "ARCHIVED": 0}
    assert s["photos"] == {"PROCESSING": 1, "READY": 2, "FAILED": 1, "HIDDEN": 0}
    assert (s["unique_visitors"], s["page_views"], s["downloads"]) == (2, 3, 1)
    assert s["failed_uploads"] == 0
    assert s["storage"]["quota_bytes"] == 2 * 1024**3 and s["storage"]["used_bytes"] > 0
    assert s["subscription"]["plan"] and s["subscription"]["status"] == "ACTIVE"


def test_hourly_chart_uses_local_hours(client, active):
    _, headers = active()
    event = new_event(client, headers)
    other = new_event(client, headers, name="Other")
    ids = [add_photo(client, headers, event["id"], process=False) for _ in range(3)]
    elsewhere = add_photo(client, headers, other["id"], process=False)
    # India is UTC+5:30.
    for public_id, utc in zip(
        [*ids, elsewhere],
        [
            "2026-12-11 19:00+00",  # 00:30 on the 12th
            "2026-12-12 04:45+00",  # 10:15
            "2026-12-12 18:45+00",  # 00:15 on the 13th: another day
            "2026-12-12 05:10+00",  # 10:40, other event
        ],
        strict=True,
    ):
        sql("UPDATE photos SET uploaded_at = :t WHERE public_id = :p", t=utc, p=public_id)

    hours = client.get("/dashboard/uploads/hourly?date=2026-12-12", headers=headers).json()
    assert len(hours) == 24
    assert {h["hour"]: h["photos"] for h in hours if h["photos"]} == {0: 1, 10: 2}
    only = client.get(
        f"/dashboard/uploads/hourly?date=2026-12-12&event_id={event['id']}", headers=headers
    ).json()
    assert {h["hour"]: h["photos"] for h in only if h["photos"]} == {0: 1, 10: 1}


def test_event_stats(client, active):
    _, headers = active()
    event = new_event(client, headers)
    original = jpeg(size=(800, 600), gps=False)
    photo = add_photo(client, headers, event["id"], original)
    add_photo(client, headers, event["id"], process=False)
    slug = event["public_slug"]

    client.get(f"/public/galleries/{slug}")
    client.get(f"/public/galleries/{slug}")  # same browser, same day
    client.get(f"/public/photos/{photo}/download", follow_redirects=False)
    client.cookies.clear()
    client.get(f"/public/galleries/{slug}")  # another browser

    r = client.get(f"/dashboard/events/{event['id']}/stats", headers=headers)
    assert r.status_code == 200, r.text
    stats = r.json()
    assert stats["photos"]["READY"] == 1 and stats["photos"]["PROCESSING"] == 1
    assert stats["total_bytes"] > len(original)
    assert (stats["unique_visitors"], stats["page_views"]) == (2, 3)
    assert stats["downloads"] == {"WEB": 1, "ORIGINAL": 0}
    assert stats["live_viewers"] == 0
    (today,) = stats["daily"]
    assert date.fromisoformat(today["date"]) == datetime.now(local_tz()).date()
    assert (today["unique_visitors"], today["page_views"]) == (2, 3)
    assert (today["photos_uploaded"], today["downloads"]) == (2, 1)


def test_returning_browser_counts_once_per_day(client, active):
    _, headers = active()
    event = new_event(client, headers)
    slug = event["public_slug"]
    client.get(f"/public/galleries/{slug}")
    # Pretend that visit was two days ago.
    sql("UPDATE gallery_visitors SET last_seen_at = now() - interval '2 days'")
    sql("DELETE FROM event_daily_stats")
    client.get(f"/public/galleries/{slug}")  # back today: unique again
    (today,) = client.get(f"/dashboard/events/{event['id']}/stats", headers=headers).json()["daily"]
    assert (today["unique_visitors"], today["page_views"]) == (1, 1)


def test_upload_history_filters(client, active):
    _, headers = active()
    event = new_event(client, headers)
    add_photo(client, headers, event["id"], process=False)
    add_photo(client, headers, event["id"], process=False)
    sql("UPDATE upload_files SET status = 'FAILED', last_error = 'mismatch' WHERE id = "
        "(SELECT min(id) FROM upload_files)")  # fmt: skip

    everything = client.get("/dashboard/upload-history", headers=headers).json()
    assert len(everything) == 2 and everything[0]["event_name"] == EVENT["name"]
    failed = client.get("/dashboard/upload-history?status=FAILED", headers=headers).json()
    assert [f["last_error"] for f in failed] == ["mismatch"]
    page = client.get("/dashboard/upload-history?limit=1&offset=1", headers=headers).json()
    assert page[0]["id"] == everything[1]["id"]


def test_dashboard_is_private_and_works_when_expired(client, active):
    _, alice = active()
    _, bob = active("bob@example.com", "+919876500000")
    event = new_event(client, alice)
    r = client.get(f"/dashboard/events/{event['id']}/stats", headers=bob)
    assert r.status_code == 404 and code(r) == "EVENT_NOT_FOUND"

    sql("UPDATE photographers SET status = 'EXPIRED'")
    assert client.get("/dashboard/summary", headers=alice).status_code == 200
