import uuid
from datetime import datetime
from enum import StrEnum

from sqlalchemy import (
    BigInteger,
    Boolean,
    CheckConstraint,
    Column,
    DateTime,
    ForeignKey,
    Identity,
    Index,
    Integer,
    SmallInteger,
    String,
    Table,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column

from veda.constants.enums import (
    EmailStatus,
    EmailTemplate,
    PaymentGatewayName,
    PaymentStatus,
    PhotographerStatus,
    SubscriptionStatus,
)
from veda.db import Base

# Supabase's login table. Declared only so foreign keys can point at it;
# Alembic skips the auth schema (see migrations/env.py), Supabase owns it.
auth_users = Table(
    "users", Base.metadata, Column("id", UUID(as_uuid=True), primary_key=True), schema="auth"
)


def _one_of(column: str, values: type[StrEnum]) -> str:
    """CHECK expression for a status column; text + CHECK is easier to migrate than PG enums."""
    return f"{column} IN ({', '.join(repr(v.value) for v in values)})"


def _created_at() -> Mapped[datetime]:
    return mapped_column(DateTime(timezone=True), server_default=func.now())


def _updated_at() -> Mapped[datetime]:
    return mapped_column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now())


# --- A. Identity ---------------------------------------------------------------


class Photographer(Base):
    __tablename__ = "photographers"
    __table_args__ = (CheckConstraint(_one_of("status", PhotographerStatus), name="status"),)

    # Same UUID as the Supabase login (auth.users.id): one account, one photographer.
    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("auth.users.id"), primary_key=True
    )
    public_id: Mapped[str] = mapped_column(String(9), unique=True)  # VP-7K3Q9M, shown to users
    full_name: Mapped[str] = mapped_column(String(120))
    studio_name: Mapped[str | None] = mapped_column(String(160))
    email: Mapped[str] = mapped_column(String(255), unique=True)  # copy of auth email, lowercase
    phone: Mapped[str] = mapped_column(String(20), unique=True)  # E.164, e.g. +919876543210
    city: Mapped[str | None] = mapped_column(String(80))
    status: Mapped[str] = mapped_column(
        String(20), server_default=PhotographerStatus.PENDING_PAYMENT, index=True
    )
    created_at: Mapped[datetime] = _created_at()
    updated_at: Mapped[datetime] = _updated_at()
    deleted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class Admin(Base):
    """A Supabase user listed here is a super-admin. Added only via `python -m veda.cli`."""

    __tablename__ = "admins"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("auth.users.id"), primary_key=True
    )
    created_at: Mapped[datetime] = _created_at()


# --- B. Plans and billing --------------------------------------------------------


class Plan(Base):
    __tablename__ = "plans"
    __table_args__ = (
        CheckConstraint("price_paise > 0", name="price_positive"),
        CheckConstraint("storage_bytes > 0", name="storage_positive"),
    )

    id: Mapped[int] = mapped_column(SmallInteger, Identity(), primary_key=True)
    code: Mapped[str] = mapped_column(String(32), unique=True)  # BASIC_2GB, ...
    name: Mapped[str] = mapped_column(String(80))
    price_paise: Mapped[int] = mapped_column(Integer)  # 499900 = Rs 4,999. Never floats.
    currency: Mapped[str] = mapped_column(String(3), server_default="INR")
    storage_bytes: Mapped[int] = mapped_column(BigInteger)
    duration_days: Mapped[int] = mapped_column(SmallInteger, server_default="365")
    is_active: Mapped[bool] = mapped_column(
        Boolean, server_default=text("true")
    )  # hide, keep history
    sort_order: Mapped[int] = mapped_column(SmallInteger, server_default="0")
    created_at: Mapped[datetime] = _created_at()


class Payment(Base):
    __tablename__ = "payments"
    __table_args__ = (
        CheckConstraint(_one_of("status", PaymentStatus), name="status"),
        CheckConstraint(_one_of("gateway", PaymentGatewayName), name="gateway"),
        Index("ix_payments_photographer_created", "photographer_id", "created_at"),
    )

    id: Mapped[int] = mapped_column(BigInteger, Identity(), primary_key=True)
    photographer_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("photographers.id"))
    plan_id: Mapped[int] = mapped_column(ForeignKey("plans.id"))
    gateway: Mapped[str] = mapped_column(String(20), server_default=PaymentGatewayName.RAZORPAY)
    gateway_order_id: Mapped[str] = mapped_column(String(64), unique=True)
    gateway_payment_id: Mapped[str | None] = mapped_column(String(64), unique=True)
    amount_paise: Mapped[int] = mapped_column(Integer)  # copied from the plan at order time
    currency: Mapped[str] = mapped_column(String(3))
    status: Mapped[str] = mapped_column(String(20), server_default=PaymentStatus.CREATED)
    method: Mapped[str | None] = mapped_column(String(32))  # upi, card, netbanking
    failure_reason: Mapped[str | None] = mapped_column(String(255))
    paid_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    gateway_response: Mapped[dict | None] = mapped_column(JSONB)
    created_at: Mapped[datetime] = _created_at()
    updated_at: Mapped[datetime] = _updated_at()


class PaymentWebhookEvent(Base):
    """Every valid webhook received. The unique event id makes Razorpay's retries harmless."""

    __tablename__ = "payment_webhook_events"

    id: Mapped[int] = mapped_column(BigInteger, Identity(), primary_key=True)
    gateway_event_id: Mapped[str] = mapped_column(String(64), unique=True)
    event_type: Mapped[str] = mapped_column(String(64))
    payload: Mapped[dict] = mapped_column(JSONB)
    processed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    error: Mapped[str | None] = mapped_column(String(255))  # e.g. ORDER_NOT_FOUND
    created_at: Mapped[datetime] = _created_at()


class Subscription(Base):
    __tablename__ = "subscriptions"
    __table_args__ = (
        CheckConstraint(_one_of("status", SubscriptionStatus), name="status"),
        # At most one ACTIVE subscription per photographer, enforced by the database.
        Index(
            "uq_subscriptions_one_active",
            "photographer_id",
            unique=True,
            postgresql_where=text("status = 'ACTIVE'"),
        ),
        Index("ix_subscriptions_status_expires", "status", "expires_at"),  # hourly expiry job
    )

    id: Mapped[int] = mapped_column(BigInteger, Identity(), primary_key=True)
    photographer_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("photographers.id"))
    plan_id: Mapped[int] = mapped_column(ForeignKey("plans.id"))
    payment_id: Mapped[int] = mapped_column(ForeignKey("payments.id"), unique=True)
    status: Mapped[str] = mapped_column(String(20), server_default=SubscriptionStatus.ACTIVE)
    storage_bytes: Mapped[int] = mapped_column(BigInteger)  # snapshot; plan edits don't change it
    starts_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = _created_at()


# --- C. Storage --------------------------------------------------------------------


class StoragePool(Base):
    """One row. Space sold = SUM(storage_quotas.quota_bytes), never more than capacity."""

    __tablename__ = "storage_pool"
    __table_args__ = (CheckConstraint("id = 1", name="single_row"),)

    id: Mapped[int] = mapped_column(SmallInteger, primary_key=True)
    capacity_bytes: Mapped[int] = mapped_column(BigInteger)  # admin-editable (Phase 8)
    updated_at: Mapped[datetime] = _updated_at()


class StorageQuota(Base):
    __tablename__ = "storage_quotas"
    __table_args__ = (
        CheckConstraint(
            "quota_bytes >= 0 AND used_bytes >= 0 AND reserved_bytes >= 0", name="non_negative"
        ),
    )

    photographer_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("photographers.id"), primary_key=True
    )
    # Active: the plan's size. Expired: shrunk to used + reserved, freeing the rest.
    quota_bytes: Mapped[int] = mapped_column(BigInteger, server_default="0")
    used_bytes: Mapped[int] = mapped_column(BigInteger, server_default="0")  # Phase 4
    reserved_bytes: Mapped[int] = mapped_column(BigInteger, server_default="0")  # Phase 4
    updated_at: Mapped[datetime] = _updated_at()


# --- G. Operations ---------------------------------------------------------------------


class EmailOutbox(Base):
    """Written in the same transaction as the business change; a worker sends it."""

    __tablename__ = "email_outbox"
    __table_args__ = (
        CheckConstraint(_one_of("status", EmailStatus), name="status"),
        CheckConstraint(_one_of("template", EmailTemplate), name="template"),
        Index("ix_email_outbox_pending", "id", postgresql_where=text("status = 'PENDING'")),
    )

    id: Mapped[int] = mapped_column(BigInteger, Identity(), primary_key=True)
    to_email: Mapped[str] = mapped_column(String(255))
    template: Mapped[str] = mapped_column(String(64))
    payload: Mapped[dict] = mapped_column(JSONB)
    status: Mapped[str] = mapped_column(String(20), server_default=EmailStatus.PENDING)
    attempts: Mapped[int] = mapped_column(SmallInteger, server_default="0")
    last_error: Mapped[str | None] = mapped_column(String(500))
    sent_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = _created_at()
