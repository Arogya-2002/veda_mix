"""Celery tasks: open a session, call a service, nothing else."""

from datetime import UTC, datetime, timedelta

from celery import Task

from veda.config.configuration import get_params
from veda.db import sync_session
from veda.integrations import storage
from veda.integrations.email import get_email_sender
from veda.pipeline.photo_processing_pipeline import PhotoProcessingPipeline
from veda.services import photos, uploads
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


@celery_app.task(name="veda.process_photo", bind=True)
def process_photo(self: Task, photo_id: int) -> str:
    try:
        return PhotoProcessingPipeline().run(photo_id)
    except Exception as e:  # S3 or DB trouble; bad images are settled inside the pipeline
        p = get_params()["processing"]
        if self.request.retries + 1 < p["max_attempts"]:
            countdown = p["retry_backoff_seconds"] * 2**self.request.retries
            raise self.retry(exc=e, countdown=countdown) from e
        with sync_session() as s, s.begin():
            photos.fail(s, photo_id, f"{type(e).__name__}: {e}")
        raise


@celery_app.task(name="veda.requeue_stuck_photos")
def requeue_stuck_photos() -> int:
    p = get_params()["processing"]
    with sync_session() as s, s.begin():
        photo_ids = photos.requeue_stuck(
            s,
            datetime.now(UTC),
            timedelta(minutes=p["requeue_after_minutes"]),
            p["max_attempts"],
        )
    for photo_id in photo_ids:  # after commit
        process_photo.delay(photo_id)
    return len(photo_ids)
