from celery import Celery

from veda.config.configuration import get_settings
from veda.logging import setup_logging

setup_logging()
_url = get_settings().redis_url

celery_app = Celery("veda", broker=_url, backend=_url)
celery_app.conf.update(
    task_default_queue="default",  # worker consumes default + ml; Celery's own default is "celery"
    task_acks_late=True,  # a killed worker's task is redelivered, not lost
    worker_prefetch_multiplier=1,
    timezone="UTC",
)


@celery_app.task(name="veda.ping")
def ping() -> str:
    return "pong"
