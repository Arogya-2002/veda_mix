"""Orders, payment activation and Razorpay webhooks.

Transactions: the request's session starts a transaction on its first query (the auth
dependency already ran one). Functions here never commit; the route commits once at the
end, so a failure anywhere rolls everything back together.

Lock order, everywhere: payment → subscription → storage quota → photographer.
Same order in every code path = no deadlocks."""

import hashlib
import json
import uuid
from datetime import UTC, datetime, timedelta

from sqlalchemy import func, select, update
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from veda.constants.enums import (
    EmailTemplate,
    PaymentStatus,
    PhotographerStatus,
    RazorpayEvent,
    SubscriptionStatus,
)
from veda.db.models import (
    EmailOutbox,
    Payment,
    PaymentWebhookEvent,
    Photographer,
    Plan,
    StoragePool,
    StorageQuota,
    Subscription,
)
from veda.exception import VedaException
from veda.integrations.payments import PaymentGateway
from veda.logging import get_logger

log = get_logger(__name__)

CAN_BUY = (PhotographerStatus.PENDING_PAYMENT, PhotographerStatus.EXPIRED)


async def pool_has_room(db: AsyncSession, photographer_id: uuid.UUID, storage_bytes: int) -> bool:
    """No overselling: all quotas together must fit in the pool."""
    capacity = await db.scalar(select(StoragePool.capacity_bytes).where(StoragePool.id == 1))
    sold_to_others = await db.scalar(
        select(func.coalesce(func.sum(StorageQuota.quota_bytes), 0)).where(
            StorageQuota.photographer_id != photographer_id
        )
    )
    return sold_to_others + storage_bytes <= capacity


async def create_order(
    db: AsyncSession, gateway: PaymentGateway, photographer: Photographer, plan_code: str
) -> tuple[Payment, Plan]:
    if photographer.status not in CAN_BUY:
        raise VedaException("ALREADY_SUBSCRIBED", "You already have an active plan", 409)
    plan = await db.scalar(select(Plan).where(Plan.code == plan_code, Plan.is_active))
    if plan is None:
        raise VedaException("PLAN_NOT_FOUND", "No such plan", 404)
    if not await pool_has_room(db, photographer.id, plan.storage_bytes):
        raise VedaException("POOL_FULL", "This plan is sold out right now", 409)

    # End the read transaction before the network call, so no DB connection sits idle
    # inside a transaction while Razorpay responds.
    await db.commit()
    # ponytail: check-then-pay race: two buyers can both pass pool_has_room when the pool is
    # nearly full; activation still honours both (they paid). Tiny overshoot, alerted below.
    order_id = await gateway.create_order(
        plan.price_paise, plan.currency, receipt=f"{photographer.public_id}-{uuid.uuid4().hex[:8]}"
    )
    payment = Payment(
        photographer_id=photographer.id,
        plan_id=plan.id,
        gateway_order_id=order_id,
        amount_paise=plan.price_paise,
        currency=plan.currency,
    )
    db.add(payment)
    return payment, plan


async def activate(
    db: AsyncSession,
    order_id: str,
    payment_id: str,
    *,
    owner_id: uuid.UUID | None = None,
    amount_paise: int | None = None,
    method: str | None = None,
    raw: dict | None = None,
) -> Subscription:
    """Mark the payment captured and grant the plan. Idempotent: the checkout callback
    and the webhook both call this; whichever comes second finds CAPTURED and returns."""
    payment = await db.scalar(
        select(Payment).where(Payment.gateway_order_id == order_id).with_for_update()
    )
    if payment is None or (owner_id is not None and payment.photographer_id != owner_id):
        raise VedaException("ORDER_NOT_FOUND", "No such order", 404)
    if payment.status == PaymentStatus.CAPTURED:
        return await db.scalar(select(Subscription).where(Subscription.payment_id == payment.id))
    if amount_paise is not None and amount_paise != payment.amount_paise:
        raise VedaException("AMOUNT_MISMATCH", "Paid amount does not match the order", 400)

    plan = await db.get(Plan, payment.plan_id)
    now = datetime.now(UTC)
    payment.status = PaymentStatus.CAPTURED
    payment.gateway_payment_id = payment_id
    payment.paid_at = now
    payment.method = method
    payment.gateway_response = raw

    # Paid twice (two tabs)? Don't lose time: the new plan starts where the old one ends.
    current = await db.scalar(
        select(Subscription)
        .where(
            Subscription.photographer_id == payment.photographer_id,
            Subscription.status == SubscriptionStatus.ACTIVE,
        )
        .with_for_update()
    )
    extend_from = now
    if current is not None:
        current.status = SubscriptionStatus.SUPERSEDED
        extend_from = max(now, current.expires_at)
        await db.flush()  # free the one-ACTIVE-per-photographer slot before inserting

    subscription = Subscription(
        photographer_id=payment.photographer_id,
        plan_id=plan.id,
        payment_id=payment.id,
        status=SubscriptionStatus.ACTIVE,
        storage_bytes=plan.storage_bytes,
        starts_at=now,
        expires_at=extend_from + timedelta(days=plan.duration_days),
    )
    db.add(subscription)

    await db.execute(
        insert(StorageQuota)
        .values(photographer_id=payment.photographer_id, quota_bytes=plan.storage_bytes)
        .on_conflict_do_update(
            index_elements=[StorageQuota.photographer_id],
            set_={"quota_bytes": plan.storage_bytes},
        )
    )
    await db.execute(
        update(Photographer)
        .where(Photographer.id == payment.photographer_id, Photographer.status.in_(CAN_BUY))
        .values(status=PhotographerStatus.ACTIVE)
    )  # conditional: a SUSPENDED photographer stays suspended even after paying

    photographer = await db.get(Photographer, payment.photographer_id)
    db.add(
        EmailOutbox(
            to_email=photographer.email,
            template=EmailTemplate.PAYMENT_RECEIPT,
            payload={
                "name": photographer.full_name,
                "plan": plan.name,
                "amount_paise": payment.amount_paise,
                "currency": payment.currency,
                "payment_id": payment_id,
                "expires_at": subscription.expires_at.isoformat(),
            },
        )
    )
    await db.flush()

    if not await pool_has_room(db, payment.photographer_id, plan.storage_bytes):
        log.warning("storage_pool_oversold", photographer_id=str(payment.photographer_id))
    log.info("subscription_activated", payment_id=payment.id, plan=plan.code)
    return subscription


async def handle_webhook(
    db: AsyncSession, gateway: PaymentGateway, body: bytes, signature: str, event_id: str | None
) -> str:
    """Returns what happened (for logs); raises only on a bad signature, so Razorpay
    stops retrying everything else."""
    if not gateway.verify_webhook(body, signature):
        log.warning("razorpay_webhook_bad_signature")  # alert-worthy: forged or misconfigured
        raise VedaException("INVALID_SIGNATURE", "Bad webhook signature", 400)

    data = json.loads(body)
    event_id = event_id or hashlib.sha256(body).hexdigest()[:64]
    event_type = data.get("event", "")

    # Insert-or-ignore, then lock: concurrent deliveries of one event run one at a time.
    await db.execute(
        insert(PaymentWebhookEvent)
        .values(gateway_event_id=event_id, event_type=event_type, payload=data)
        .on_conflict_do_nothing(index_elements=[PaymentWebhookEvent.gateway_event_id])
    )
    event = await db.scalar(
        select(PaymentWebhookEvent)
        .where(PaymentWebhookEvent.gateway_event_id == event_id)
        .with_for_update()
    )
    if event.processed_at is not None:
        return "duplicate"

    outcome = "ignored"
    try:
        entity = data["payload"]["payment"]["entity"]
        if event_type == RazorpayEvent.PAYMENT_CAPTURED:
            await activate(
                db,
                entity["order_id"],
                entity["id"],
                amount_paise=entity.get("amount"),
                method=entity.get("method"),
                raw=entity,
            )
            outcome = "activated"
        elif event_type == RazorpayEvent.PAYMENT_FAILED:
            await db.execute(
                update(Payment)
                .where(
                    Payment.gateway_order_id == entity["order_id"],
                    Payment.status == PaymentStatus.CREATED,
                )
                .values(status=PaymentStatus.FAILED, failure_reason=entity.get("error_description"))
            )
            outcome = "failed"
    except VedaException as e:  # unknown order, amount mismatch: keep a record, don't retry
        event.error = e.code
        outcome = e.code
        log.error("razorpay_webhook_rejected", code=e.code, event_id=event_id)
    except (KeyError, TypeError):
        event.error = outcome = "MALFORMED"

    event.processed_at = datetime.now(UTC)
    return outcome
