"""Every status and fixed choice in the system. DB columns store the string values,
enforced by CHECK constraints built from these same enums (see db/models.py)."""

from enum import StrEnum


class PhotographerStatus(StrEnum):
    PENDING_PAYMENT = "PENDING_PAYMENT"  # registered, never paid
    ACTIVE = "ACTIVE"  # has an active subscription
    SUSPENDED = "SUSPENDED"  # blocked by an admin
    EXPIRED = "EXPIRED"  # subscription ended: galleries stay viewable, uploads stop
    DELETED = "DELETED"


class PaymentGatewayName(StrEnum):
    RAZORPAY = "RAZORPAY"


class PaymentStatus(StrEnum):
    CREATED = "CREATED"  # order created, not paid yet
    CAPTURED = "CAPTURED"  # money received; subscription activated
    FAILED = "FAILED"
    REFUNDED = "REFUNDED"


class SubscriptionStatus(StrEnum):
    ACTIVE = "ACTIVE"
    EXPIRED = "EXPIRED"
    SUPERSEDED = "SUPERSEDED"  # replaced by a newer subscription before it expired


class EmailStatus(StrEnum):
    PENDING = "PENDING"
    SENT = "SENT"
    FAILED = "FAILED"  # gave up after MAX_EMAIL_ATTEMPTS


class EmailTemplate(StrEnum):
    PAYMENT_RECEIPT = "payment_receipt"


class RazorpayEvent(StrEnum):
    """Webhook event types we act on; others are logged and ignored."""

    PAYMENT_CAPTURED = "payment.captured"
    PAYMENT_FAILED = "payment.failed"
