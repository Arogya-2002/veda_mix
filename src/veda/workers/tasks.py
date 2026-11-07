"""Celery tasks: open a session, call a service, nothing else."""

from datetime import UTC, datetime

from veda.db import sync_session
from veda.integrations import storage
from veda.integrations.email import get_email_sender
from veda.services import uploads
from veda.services.outbox import send_pending_emails
from veda.services.subscriptions import expire_due_subscriptions
from veda.workers import celery_app


@celery_app.task(name="veda.ping")
def ping() -> str:
    return "pong"


@celery_app.task(name="veda.send_pending_emails")
def send_pending_emails_task() -> int:
    with sync_session() as s, s.begin():
        return send_pending_emails(s, get_email_sender())


@celery_app.task(name="veda.expire_subscriptions")
def expire_subscriptions_task() -> int:
    with sync_session() as s, s.begin():
        return expire_due_subscriptions(s, datetime.now(UTC))


@celery_app.task(name="veda.release_expired_uploads")
def release_expired_uploads() -> int:
    with sync_session() as s, s.begin():
        keys = uploads.release_expired(s, datetime.now(UTC))
    for key in keys:  # after commit: if a delete fails, the space is already free
        storage.delete(key)
    return len(keys)
