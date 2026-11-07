import json
from datetime import UTC, datetime, timedelta

from sqlalchemy import text

from tests.conftest import sign
from veda.constants.enums import EmailTemplate
from veda.services.outbox import MAX_EMAIL_ATTEMPTS, send_pending_emails
from veda.services.subscriptions import expire_due_subscriptions

GiB = 1024**3


def code(r):
    return r.json()["error"]["code"]


def order(client, headers, plan="BASIC_2GB"):
    r = client.post("/payments/orders", json={"plan_code": plan}, headers=headers)
    assert r.status_code == 201, r.text
    return r.json()


def checkout_result(order_id, payment_id="pay_123"):
    """What Razorpay checkout hands the browser after a successful payment."""
    return {
        "razorpay_order_id": order_id,
        "razorpay_payment_id": payment_id,
        "razorpay_signature": sign("test_key_secret", f"{order_id}|{payment_id}".encode()),
    }


def webhook(client, event, order_id, *, payment_id="pay_123", amount=499900, event_id="evt_1"):
    body = json.dumps(
        {
            "event": event,
            "payload": {
                "payment": {
                    "entity": {
                        "id": payment_id,
                        "order_id": order_id,
                        "amount": amount,
                        "method": "upi",
                        "error_description": "Card declined",
                    }
                }
            },
        }
    ).encode()
    return client.post(
        "/webhooks/razorpay",
        content=body,
        headers={
            "x-razorpay-signature": sign("test_webhook_secret", body),
            "x-razorpay-event-id": event_id,
        },
    )


def count(db, sql, **params):
    with db() as s:
        return s.execute(text(sql), params).scalar()


# --- plans and orders -------------------------------------------------------------


def test_plans_are_public_and_sorted(client, db):
    plans = client.get("/plans").json()
    assert [(p["code"], p["price_paise"], p["storage_bytes"]) for p in plans] == [
        ("BASIC_2GB", 499900, 2 * GiB),
        ("STANDARD_5GB", 699900, 5 * GiB),
        ("PREMIUM_10GB", 899900, 10 * GiB),
    ]


def test_create_order_uses_server_price(client, photographer, gateway):
    _, headers = photographer()
    body = order(client, headers, "PREMIUM_10GB")
    assert body["amount_paise"] == 899900 and body["key_id"] == "rzp_test_key"
    assert gateway.orders[0]["amount"] == 899900  # the price came from our DB, not the client


def test_unknown_plan(client, photographer, gateway):
    _, headers = photographer()
    r = client.post("/payments/orders", json={"plan_code": "FREE_1TB"}, headers=headers)
    assert r.status_code == 404 and code(r) == "PLAN_NOT_FOUND"


def test_pool_full_blocks_new_orders(client, photographer, gateway, db):
    with db() as s, s.begin():
        s.execute(text("UPDATE storage_pool SET capacity_bytes = :c"), {"c": 3 * GiB})
    _, first = photographer("a@example.com", "+911111111111")
    client.post(
        "/payments/verify", json=checkout_result(order(client, first)["order_id"]), headers=first
    )

    _, second = photographer("b@example.com", "+912222222222")
    r = client.post("/payments/orders", json={"plan_code": "BASIC_2GB"}, headers=second)
    assert r.status_code == 409 and code(r) == "POOL_FULL"  # 2 GB sold + 2 GB > 3 GB


# --- checkout verification --------------------------------------------------------


def test_verify_activates_everything(client, photographer, gateway, db):
    uid, headers = photographer()
    order_id = order(client, headers)["order_id"]

    r = client.post("/payments/verify", json=checkout_result(order_id), headers=headers)
    assert r.status_code == 200, r.text
    sub = r.json()
    assert sub["status"] == "ACTIVE" and sub["storage_bytes"] == 2 * GiB
    lasts = datetime.fromisoformat(sub["expires_at"]) - datetime.fromisoformat(sub["starts_at"])
    assert lasts == timedelta(days=365)

    assert client.get("/me", headers=headers).json()["status"] == "ACTIVE"
    assert client.get("/me/subscription", headers=headers).json() == sub
    assert client.get("/me/payments", headers=headers).json()[0]["status"] == "CAPTURED"
    assert (
        count(db, "SELECT quota_bytes FROM storage_quotas WHERE photographer_id=:id", id=uid)
        == 2 * GiB
    )
    assert count(db, "SELECT count(*) FROM email_outbox WHERE template='payment_receipt'") == 1


def test_verify_rejects_forged_signature(client, photographer, gateway, db):
    _, headers = photographer()
    forged = checkout_result(order(client, headers)["order_id"]) | {"razorpay_signature": "0" * 64}
    r = client.post("/payments/verify", json=forged, headers=headers)
    assert r.status_code == 400 and code(r) == "INVALID_SIGNATURE"
    assert client.get("/me", headers=headers).json()["status"] == "PENDING_PAYMENT"


def test_cannot_verify_someone_elses_order(client, photographer, gateway):
    _, alice = photographer("a@example.com", "+911111111111")
    _, bob = photographer("b@example.com", "+912222222222")
    order_id = order(client, alice)["order_id"]
    r = client.post("/payments/verify", json=checkout_result(order_id), headers=bob)
    assert r.status_code == 404 and code(r) == "ORDER_NOT_FOUND"


def test_cannot_buy_while_active(client, photographer, gateway):
    _, headers = photographer()
    client.post(
        "/payments/verify",
        json=checkout_result(order(client, headers)["order_id"]),
        headers=headers,
    )
    r = client.post("/payments/orders", json={"plan_code": "BASIC_2GB"}, headers=headers)
    assert r.status_code == 409 and code(r) == "ALREADY_SUBSCRIBED"


# --- webhooks (pre-production checklist: replay 5 times -> one subscription) ---------


def test_webhook_replayed_five_times_creates_one_subscription(client, photographer, gateway, db):
    _, headers = photographer()
    order_id = order(client, headers)["order_id"]
    outcomes = [webhook(client, "payment.captured", order_id).json()["status"] for _ in range(5)]
    assert outcomes == ["activated"] + ["duplicate"] * 4
    assert count(db, "SELECT count(*) FROM subscriptions") == 1
    assert count(db, "SELECT count(*) FROM email_outbox") == 1


def test_checkout_then_webhook_activates_once(client, photographer, gateway, db):
    _, headers = photographer()
    order_id = order(client, headers)["order_id"]
    client.post("/payments/verify", json=checkout_result(order_id), headers=headers)
    # Different event id (a genuinely new delivery), same payment: still one subscription.
    assert webhook(client, "payment.captured", order_id, event_id="evt_2").status_code == 200
    assert count(db, "SELECT count(*) FROM subscriptions") == 1


def test_webhook_activates_when_user_closed_the_tab(client, photographer, gateway):
    _, headers = photographer()
    order_id = order(client, headers)["order_id"]  # never calls /payments/verify
    webhook(client, "payment.captured", order_id)
    assert client.get("/me", headers=headers).json()["status"] == "ACTIVE"


def test_webhook_bad_signature_stores_nothing(client, db):
    r = client.post(
        "/webhooks/razorpay",
        content=b'{"event":"payment.captured"}',
        headers={"x-razorpay-signature": "forged"},
    )
    assert r.status_code == 400 and code(r) == "INVALID_SIGNATURE"
    assert count(db, "SELECT count(*) FROM payment_webhook_events") == 0


def test_webhook_amount_mismatch_does_not_activate(client, photographer, gateway, db):
    _, headers = photographer()
    order_id = order(client, headers)["order_id"]
    assert (
        webhook(client, "payment.captured", order_id, amount=100).json()["status"]
        == "AMOUNT_MISMATCH"
    )
    assert count(db, "SELECT count(*) FROM subscriptions") == 0


def test_webhook_payment_failed(client, photographer, gateway):
    _, headers = photographer()
    order_id = order(client, headers)["order_id"]
    webhook(client, "payment.failed", order_id)
    assert client.get("/me/payments", headers=headers).json()[0]["status"] == "FAILED"


# --- expiry and renewal ------------------------------------------------------------


def test_expiry_makes_read_only_and_frees_unused_quota(client, photographer, gateway, db):
    uid, headers = photographer()
    client.post(
        "/payments/verify",
        json=checkout_result(order(client, headers)["order_id"]),
        headers=headers,
    )
    with db() as s, s.begin():
        s.execute(text("UPDATE storage_quotas SET used_bytes = :u"), {"u": GiB // 2})
        assert expire_due_subscriptions(s, datetime.now(UTC) + timedelta(days=366)) == 1

    assert client.get("/me", headers=headers).json()["status"] == "EXPIRED"
    assert client.get("/me/subscription", headers=headers).json()["status"] == "EXPIRED"
    quota = count(db, "SELECT quota_bytes FROM storage_quotas WHERE photographer_id=:id", id=uid)
    assert quota == GiB // 2  # shrunk to what's used; the other 1.5 GB is sellable again

    # Renewing works and restores the full quota.
    client.post(
        "/payments/verify",
        json=checkout_result(order(client, headers)["order_id"], "pay_2"),
        headers=headers,
    )
    assert client.get("/me", headers=headers).json()["status"] == "ACTIVE"
    assert (
        count(db, "SELECT quota_bytes FROM storage_quotas WHERE photographer_id=:id", id=uid)
        == 2 * GiB
    )


def test_expiry_ignores_subscriptions_not_yet_due(client, photographer, gateway, db):
    _, headers = photographer()
    client.post(
        "/payments/verify",
        json=checkout_result(order(client, headers)["order_id"]),
        headers=headers,
    )
    with db() as s, s.begin():
        assert expire_due_subscriptions(s, datetime.now(UTC)) == 0


# --- email outbox --------------------------------------------------------------------


class RecordingSender:
    """Test implementation of the EmailSender interface."""

    def __init__(self, fail=False):
        self.sent, self.fail = [], fail

    def send(self, to, template, payload):
        if self.fail:
            raise ConnectionError("SMTP down")
        self.sent.append((to, template, payload))


def test_outbox_sends_receipt_once(client, photographer, gateway, db):
    _, headers = photographer()
    client.post(
        "/payments/verify",
        json=checkout_result(order(client, headers)["order_id"]),
        headers=headers,
    )
    sender = RecordingSender()
    with db() as s, s.begin():
        assert send_pending_emails(s, sender) == 1
    with db() as s, s.begin():
        assert send_pending_emails(s, sender) == 0  # already SENT
    to, template, payload = sender.sent[0]
    assert to == "pat@example.com" and template == EmailTemplate.PAYMENT_RECEIPT
    assert payload["amount_paise"] == 499900 and payload["plan"] == "Basic 2 GB"


def test_outbox_retries_then_gives_up(client, photographer, gateway, db):
    _, headers = photographer()
    client.post(
        "/payments/verify",
        json=checkout_result(order(client, headers)["order_id"]),
        headers=headers,
    )
    for _ in range(MAX_EMAIL_ATTEMPTS):
        with db() as s, s.begin():
            send_pending_emails(s, RecordingSender(fail=True))
    assert count(db, "SELECT status FROM email_outbox") == "FAILED"
    assert count(db, "SELECT last_error FROM email_outbox") == "SMTP down"
