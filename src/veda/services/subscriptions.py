"""Subscription expiry (sync: runs in a Celery worker)."""

from datetime import datetime

from sqlalchemy import select, update
from sqlalchemy.orm import Session

from veda.constants.enums import PhotographerStatus, SubscriptionStatus
from veda.db.models import Photographer, StorageQuota, Subscription


def expire_due_subscriptions(s: Session, now: datetime) -> int:
    """Expired = read-only: galleries stay up, uploads stop, unused quota returns to the pool."""
    due = s.scalars(
        select(Subscription)
        .where(Subscription.status == SubscriptionStatus.ACTIVE, Subscription.expires_at <= now)
        .with_for_update(skip_locked=True)  # a second worker skips rows the first is handling
    ).all()
    for sub in due:  # lock order: subscription → quota → photographer (same as activation)
        sub.status = SubscriptionStatus.EXPIRED
        s.execute(
            update(StorageQuota)
            .where(StorageQuota.photographer_id == sub.photographer_id)
            .values(quota_bytes=StorageQuota.used_bytes + StorageQuota.reserved_bytes)
        )
        s.execute(
            update(Photographer)
            .where(
                Photographer.id == sub.photographer_id,
                Photographer.status == PhotographerStatus.ACTIVE,
            )
            .values(status=PhotographerStatus.EXPIRED)
        )
    return len(due)
