from datetime import datetime
from typing import Annotated

from fastapi import APIRouter, Depends, Request
from pydantic import BaseModel, ConfigDict
from sqlalchemy import select

from veda.api.deps import CurrentPhotographer, DbSession
from veda.constants.enums import SubscriptionStatus
from veda.db.models import Payment, Plan, Subscription
from veda.exception import VedaException
from veda.integrations.payments import PaymentGateway, get_payment_gateway
from veda.services import payments

router = APIRouter(tags=["payments"])
Gateway = Annotated[PaymentGateway, Depends(get_payment_gateway)]


class PlanOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    code: str
    name: str
    price_paise: int
    currency: str
    storage_bytes: int
    duration_days: int


class OrderIn(BaseModel):
    plan_code: str


class OrderOut(BaseModel):
    """Everything the React Razorpay checkout needs."""

    order_id: str
    amount_paise: int
    currency: str
    key_id: str
    plan_code: str


class VerifyIn(BaseModel):
    """Field names exactly as Razorpay checkout returns them."""

    razorpay_order_id: str
    razorpay_payment_id: str
    razorpay_signature: str


class SubscriptionOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    status: str
    storage_bytes: int
    starts_at: datetime
    expires_at: datetime


class PaymentOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    gateway_order_id: str
    amount_paise: int
    currency: str
    status: str
    method: str | None
    paid_at: datetime | None
    created_at: datetime


@router.get("/plans", response_model=list[PlanOut])
async def list_plans(db: DbSession):
    return (await db.scalars(select(Plan).where(Plan.is_active).order_by(Plan.sort_order))).all()


@router.post("/payments/orders", status_code=201, response_model=OrderOut)
async def create_order(
    body: OrderIn, photographer: CurrentPhotographer, db: DbSession, gateway: Gateway
):
    payment, plan = await payments.create_order(db, gateway, photographer, body.plan_code)
    await db.commit()
    return OrderOut(
        order_id=payment.gateway_order_id,
        amount_paise=payment.amount_paise,
        currency=payment.currency,
        key_id=gateway.key_id,
        plan_code=plan.code,
    )


@router.post("/payments/verify", response_model=SubscriptionOut)
async def verify_payment(
    body: VerifyIn, photographer: CurrentPhotographer, db: DbSession, gateway: Gateway
):
    if not gateway.verify_payment(
        body.razorpay_order_id, body.razorpay_payment_id, body.razorpay_signature
    ):
        raise VedaException("INVALID_SIGNATURE", "Payment could not be verified", 400)
    subscription = await payments.activate(
        db, body.razorpay_order_id, body.razorpay_payment_id, owner_id=photographer.id
    )
    await db.commit()
    return subscription


@router.post("/webhooks/razorpay")
async def razorpay_webhook(request: Request, db: DbSession, gateway: Gateway):
    outcome = await payments.handle_webhook(
        db,
        gateway,
        body=await request.body(),  # raw bytes: the signature covers them exactly
        signature=request.headers.get("x-razorpay-signature", ""),
        event_id=request.headers.get("x-razorpay-event-id"),
    )
    await db.commit()
    return {"status": outcome}


@router.get("/me/subscription", response_model=SubscriptionOut | None)
async def my_subscription(photographer: CurrentPhotographer, db: DbSession):
    """The active subscription, else the most recent one, else null."""
    return await db.scalar(
        select(Subscription)
        .where(Subscription.photographer_id == photographer.id)
        .order_by((Subscription.status == SubscriptionStatus.ACTIVE).desc(), Subscription.id.desc())
        .limit(1)
    )


@router.get("/me/payments", response_model=list[PaymentOut])
async def my_payments(photographer: CurrentPhotographer, db: DbSession):
    return (
        await db.scalars(
            select(Payment)
            .where(Payment.photographer_id == photographer.id)
            .order_by(Payment.id.desc())
        )
    ).all()
