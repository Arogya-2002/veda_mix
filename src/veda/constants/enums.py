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


class EventStatus(StrEnum):
    UPCOMING = "UPCOMING"  # created; guests can already open the gallery
    LIVE = "LIVE"  # the event is happening; uploads stream in
    COMPLETED = "COMPLETED"  # uploads blocked, gallery stays viewable
    ARCHIVED = "ARCHIVED"  # hidden from guests


class EventVisibility(StrEnum):
    PUBLIC = "PUBLIC"  # anyone with the QR / link
    PIN_PROTECTED = "PIN_PROTECTED"  # link + event PIN
    PRIVATE = "PRIVATE"  # closed to guests; only the photographer sees it


class DownloadQuality(StrEnum):
    WEB = "WEB"  # resized copy
    ORIGINAL = "ORIGINAL"


class UploadSource(StrEnum):
    BROWSER = "BROWSER"
    FOLDER_WATCH = "FOLDER_WATCH"  # desktop app watching the camera's output folder
    ANDROID_USB = "ANDROID_USB"  # camera tethered to a phone
    WINDOWS_BRIDGE = "WINDOWS_BRIDGE"
    API = "API"


class UploadFileStatus(StrEnum):
    PENDING = "PENDING"  # space reserved, waiting for the file to reach S3
    FAILED = "FAILED"  # S3 copy didn't match; can retry, space stays reserved
    COMPLETED = "COMPLETED"  # verified; a photo row exists
    CANCELLED = "CANCELLED"  # aborted by the client; space released
    EXPIRED = "EXPIRED"  # reservation timed out; space released


class UploadDecision(StrEnum):
    """Per-file answer to an upload request."""

    UPLOAD = "UPLOAD"  # go ahead: PUT to the URL given
    DUPLICATE = "DUPLICATE"  # same bytes already in this event
    QUOTA_EXCEEDED = "QUOTA_EXCEEDED"


class PhotoStatus(StrEnum):
    PROCESSING = "PROCESSING"  # original stored; thumbnails pending (Phase 5)
    READY = "READY"  # visible in the gallery
    FAILED = "FAILED"
    HIDDEN = "HIDDEN"  # hidden by the photographer


class ActorType(StrEnum):
    PHOTOGRAPHER = "PHOTOGRAPHER"
    ADMIN = "ADMIN"
    SYSTEM = "SYSTEM"  # workers, beat jobs


class AuditAction(StrEnum):
    """No DB CHECK on this one: new actions shouldn't need a migration."""

    EVENT_CREATE = "event.create"
    EVENT_UPDATE = "event.update"
    EVENT_STATUS = "event.status"
    EVENT_NEW_LINK = "event.new_link"
    EVENT_DELETE = "event.delete"
