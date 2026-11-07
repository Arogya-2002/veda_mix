"""Email outbox sender (sync: runs in a Celery worker)."""

from datetime import UTC, datetime

from sqlalchemy import select
from sqlalchemy.orm import Session

from veda.constants.enums import EmailStatus, EmailTemplate
from veda.db.models import EmailOutbox
from veda.integrations.email import EmailSender
from veda.logging import get_logger

log = get_logger(__name__)

MAX_EMAIL_ATTEMPTS = 5


def send_pending_emails(s: Session, sender: EmailSender, limit: int = 50) -> int:
    rows = s.scalars(
        select(EmailOutbox)
        .where(EmailOutbox.status == EmailStatus.PENDING)
        .order_by(EmailOutbox.id)
        .limit(limit)
        .with_for_update(skip_locked=True)  # two workers never send the same email
    ).all()
    sent = 0
    for row in rows:
        row.attempts += 1
        try:
            sender.send(row.to_email, EmailTemplate(row.template), row.payload)
        except Exception as e:  # noqa: BLE001 - any provider error: retry next run
            row.last_error = str(e)[:500]
            if row.attempts >= MAX_EMAIL_ATTEMPTS:
                row.status = EmailStatus.FAILED
                log.error("email_gave_up", email_id=row.id, error=row.last_error)
            continue
        row.status = EmailStatus.SENT
        row.sent_at = datetime.now(UTC)
        sent += 1
    return sent
