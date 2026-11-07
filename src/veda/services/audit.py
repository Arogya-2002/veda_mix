import ipaddress
import uuid
from dataclasses import dataclass

from fastapi import Request
from fastapi.encoders import jsonable_encoder
from sqlalchemy.ext.asyncio import AsyncSession

from veda.constants.enums import ActorType, AuditAction
from veda.db.models import AuditLog


@dataclass(frozen=True)
class Actor:
    type: ActorType
    id: uuid.UUID | None
    ip: str | None = None

    @classmethod
    def from_request(cls, type: ActorType, id: uuid.UUID, request: Request) -> "Actor":
        host = request.client.host if request.client else None
        try:
            ip = str(ipaddress.ip_address(host)) if host else None
        except ValueError:  # e.g. "testclient"
            ip = None
        return cls(type, id, ip)


def record(
    db: AsyncSession,
    actor: Actor,
    action: AuditAction,
    entity_type: str,
    entity_id: object,
    before: dict | None = None,
    after: dict | None = None,
) -> None:
    """Added to the caller's transaction: the log row commits or rolls back with the change."""
    db.add(
        AuditLog(
            actor_type=actor.type,
            actor_id=actor.id,
            action=action,
            entity_type=entity_type,
            entity_id=str(entity_id),
            before_state=jsonable_encoder(before),
            after_state=jsonable_encoder(after),
            ip_address=actor.ip,
        )
    )
