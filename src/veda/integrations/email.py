"""Email sender interface. Console now; add an SesEmailSender at go-live and
return it from get_email_sender() — nothing else changes."""

from functools import lru_cache
from typing import Protocol

from veda.constants.enums import EmailTemplate
from veda.logging import get_logger

log = get_logger(__name__)


class EmailSender(Protocol):
    def send(self, to: str, template: EmailTemplate, payload: dict) -> None:
        """Send one email; raise on failure so the outbox retries it."""
        ...


class ConsoleEmailSender:
    """Development: writes the email to the logs instead of sending it."""

    def send(self, to: str, template: EmailTemplate, payload: dict) -> None:
        log.info("email_sent_to_console", to=to, template=template, payload=payload)


@lru_cache
def get_email_sender() -> EmailSender:
    return ConsoleEmailSender()
