import os

import pytest

from tests.test_dashboard import pay
from tests.test_gallery import add_photo, fresh_guest, new_event, sql  # noqa: F401

pytestmark = pytest.mark.usefixtures("s3", "fresh_guest")

GiB = 1024**3


def code(r):
    return r.json()["error"]["code"]


@pytest.fixture
def admin(new_user):
    """A super-admin who passed MFA: (id, auth header)."""
    user_id, headers = new_user("root@example.com", aal="aal2")
    sql("INSERT INTO admins (id) VALUES (:id)", id=user_id)
    return user_id, headers


def test_admin_routes_need_an_mfa_verified_admin(client, active, new_user):
    _, photographer = active()
    r = client.get("/admin/photographers", headers=photographer)
    assert r.status_code == 403 and code(r) == "FORBIDDEN"

    user_id, no_mfa = new_user("half@example.com")  # aal1
    sql("INSERT INTO admins (id) VALUES (:id)", id=user_id)
    r = client.get("/admin/storage", headers=no_mfa)
    assert r.status_code == 403 and code(r) == "MFA_REQUIRED"


def test_list_and_search_photographers(client, admin, active, photographer, gateway):
    _, root = admin
    _, paid = photographer()
    pay(client, paid)
    active("bob@example.com", "+919876500000")

    rows = client.get("/admin/photographers", headers=root).json()
    assert [r["email"] for r in rows] == ["bob@example.com", "pat@example.com"]  # newest first
    pat = rows[1]
    assert pat["plan"] and pat["subscription_expires_at"] and pat["quota_bytes"] == 2 * GiB

    found = client.get("/admin/photographers?q=BOB", headers=root).json()
    assert [r["email"] for r in found] == ["bob@example.com"]
    found = client.get(f"/admin/photographers?q={pat['public_id']}", headers=root).json()
    assert [r["email"] for r in found] == ["pat@example.com"]
    assert len(client.get("/admin/photographers?limit=1", headers=root).json()) == 1


def test_suspend_and_reactivate(client, admin, photographer, gateway):
    admin_id, root = admin
    user_id, headers = photographer()
    pay(client, headers)
    event = new_event(client, headers)

    r = client.post(f"/admin/photographers/{user_id}/suspend", headers=root)
    assert r.status_code == 200 and r.json()["status"] == "SUSPENDED"
    assert code(client.get("/me", headers=headers)) == "ACCOUNT_DISABLED"
    assert client.get(f"/public/galleries/{event['public_slug']}").status_code == 403
    assert client.post(f"/admin/photographers/{user_id}/suspend", headers=root).status_code == 409

    r = client.post(f"/admin/photographers/{user_id}/reactivate", headers=root)
    assert r.json()["status"] == "ACTIVE"  # still has a paid subscription
    assert client.get("/me", headers=headers).status_code == 200
    assert (
        client.post(f"/admin/photographers/{user_id}/reactivate", headers=root).status_code == 409
    )

    logs = client.get(
        f"/admin/audit-logs?entity_type=photographer&entity_id={user_id}", headers=root
    ).json()
    assert [entry["action"] for entry in logs] == [
        "photographer.reactivate",
        "photographer.suspend",
    ]
    assert logs[0]["actor_type"] == "ADMIN" and logs[0]["actor_id"] == str(admin_id)


def test_reactivate_unpaid_goes_back_to_pending(client, admin, photographer):
    _, root = admin
    user_id, _ = photographer()
    client.post(f"/admin/photographers/{user_id}/suspend", headers=root)
    r = client.post(f"/admin/photographers/{user_id}/reactivate", headers=root)
    assert r.json()["status"] == "PENDING_PAYMENT"


def test_quota_changes_respect_usage_and_the_pool(client, admin, active):
    _, root = admin
    user_id, headers = active()
    add_photo(client, headers, new_event(client, headers)["id"])
    used = sql("SELECT used_bytes FROM storage_quotas")[0][0]

    r = client.patch(
        f"/admin/photographers/{user_id}/quota", json={"quota_bytes": 5 * GiB}, headers=root
    )
    assert r.status_code == 200 and r.json()["quota_bytes"] == 5 * GiB
    assert client.get("/me/storage", headers=headers).json()["quota_bytes"] == 5 * GiB

    r = client.patch(
        f"/admin/photographers/{user_id}/quota", json={"quota_bytes": used - 1}, headers=root
    )
    assert r.status_code == 409 and code(r) == "QUOTA_BELOW_USAGE"
    r = client.patch(
        f"/admin/photographers/{user_id}/quota", json={"quota_bytes": 101 * GiB}, headers=root
    )
    assert r.status_code == 409 and code(r) == "POOL_FULL"
    unknown = "00000000-0000-0000-0000-000000000000"
    r = client.patch(f"/admin/photographers/{unknown}/quota", json={"quota_bytes": 1}, headers=root)
    assert r.status_code == 404

    (entry,) = client.get("/admin/audit-logs?entity_type=photographer", headers=root).json()
    assert entry["before_state"] == {"quota_bytes": 2 * GiB}
    assert entry["after_state"] == {"quota_bytes": 5 * GiB}


def test_storage_pool(client, admin, active):
    _, root = admin
    active(quota_bytes=30 * GiB)
    overview = client.get("/admin/storage", headers=root).json()
    assert overview["capacity_bytes"] == 100 * GiB
    assert (overview["sold_bytes"], overview["unsold_bytes"]) == (30 * GiB, 70 * GiB)
    assert overview["photographers_with_quota"] == 1

    r = client.patch("/admin/storage/pool", json={"capacity_bytes": 20 * GiB}, headers=root)
    assert r.status_code == 409 and code(r) == "POOL_BELOW_SOLD"
    r = client.patch("/admin/storage/pool", json={"capacity_bytes": 200 * GiB}, headers=root)
    assert r.status_code == 200 and r.json()["unsold_bytes"] == 170 * GiB
    (entry,) = client.get("/admin/audit-logs?entity_type=storage_pool", headers=root).json()
    assert entry["action"] == "pool.resize"


def test_payments_list(client, admin, photographer, gateway):
    _, root = admin
    user_id, headers = photographer()
    pay(client, headers)
    _, bob = photographer("bob@example.com", "+919876500000")
    client.post("/payments/orders", json={"plan_code": "BASIC_2GB"}, headers=bob)  # unpaid

    rows = client.get("/admin/payments", headers=root).json()
    assert len(rows) == 2 and {r["status"] for r in rows} == {"CAPTURED", "CREATED"}
    captured = client.get("/admin/payments?status=CAPTURED", headers=root).json()
    assert len(captured) == 1 and captured[0]["plan"] == "BASIC_2GB"
    assert captured[0]["photographer_name"] == "Pat Kumar"
    mine = client.get(f"/admin/payments?photographer_id={user_id}", headers=root).json()
    assert [r["status"] for r in mine] == ["CAPTURED"]


def test_live_events_and_failures(client, admin, active):
    _, root = admin
    _, headers = active()
    live = new_event(client, headers)
    client.post(f"/events/{live['id']}/go-live", headers=headers)
    new_event(client, headers, name="Not live yet")
    add_photo(client, headers, live["id"])
    broken = add_photo(client, headers, live["id"], os.urandom(3000))  # worker rejects it
    add_photo(client, headers, live["id"], process=False)
    sql("UPDATE upload_files SET status = 'FAILED', last_error = 'size mismatch' WHERE id = "
        "(SELECT max(id) FROM upload_files)")  # fmt: skip

    (event,) = client.get("/admin/events/live", headers=root).json()
    assert event["id"] == live["id"] and event["ready_photos"] == 1
    assert event["live_viewers"] == 0 and event["photographer_public_id"].startswith("VP-")

    failed = client.get("/admin/uploads/failed", headers=root).json()
    assert [u["last_error"] for u in failed["uploads"]] == ["size mismatch"]
    (photo,) = failed["photos"]
    assert photo["public_id"] == broken and photo["last_error"] == "Not a readable image"
