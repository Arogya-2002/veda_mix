from celery import Celery
from celery.schedules import crontab

from veda.config.configuration import get_settings
from veda.logging import get_logger, setup_logging

setup_logging()
_url = get_settings().redis_url

celery_app = Celery("veda", broker=_url, backend=_url, include=["veda.workers.tasks"])
celery_app.conf.update(
    task_default_queue="default",  # worker consumes default + ml; Celery's own default is "celery"
    task_acks_late=True,  # a killed worker's task is redelivered, not lost
    worker_prefetch_multiplier=1,
    timezone="UTC",
    beat_schedule={
        "send-pending-emails": {"task": "veda.send_pending_emails", "schedule": 30.0},
        "release-expired-uploads": {"task": "veda.release_expired_uploads", "schedule": 300.0},
        "requeue-stuck-photos": {"task": "veda.requeue_stuck_photos", "schedule": 600.0},
        "expire-subscriptions": {
            "task": "veda.expire_subscriptions",
            "schedule": crontab(minute=0),
        },
    },
)

log = get_logger(__name__)


def enqueue_photo_processing(photo_id: int) -> None:
    """Called by the API after its commit. Never fails the request: if Redis is down,
    the requeue-stuck-photos sweep picks the photo up later. Blocking."""
    try:
        celery_app.send_task("veda.process_photo", args=[photo_id])
    except Exception:
        log.warning("enqueue_failed", photo_id=photo_id, exc_info=True)
