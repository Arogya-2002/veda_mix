"""Every query here takes photographer_id: another photographer's event is simply not found."""

import uuid
from datetime import UTC, datetime

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from veda.constants.enums import AuditAction, EventStatus, EventVisibility
from veda.db.models import Event
from veda.exception import VedaException
from veda.services import audit
from veda.services.audit import Actor
from veda.utils.common import hash_pin, new_event_slug

# target status -> statuses it may be reached from
TRANSITIONS = {
    EventStatus.LIVE: {EventStatus.UPCOMING},
    EventStatus.COMPLETED: {EventStatus.UPCOMING, EventStatus.LIVE},
    EventStatus.ARCHIVED: {EventStatus.COMPLETED},
}
_STAMPS = {EventStatus.LIVE: "went_live_at", EventStatus.COMPLETED: "completed_at"}


async def get_owned(
    db: AsyncSession, photographer_id: uuid.UUID, event_id: int, *, lock: bool = False
) -> Event:
    query = select(Event).where(
        Event.id == event_id,
        Event.photographer_id == photographer_id,
        Event.deleted_at.is_(None),
    )
    event = await db.scalar(query.with_for_update() if lock else query)
    if event is None:  # 404, not 403: don't reveal that the id exists
        raise VedaException("EVENT_NOT_FOUND", "Event not found", 404)
    return event


async def list_owned(
    db: AsyncSession, photographer_id: uuid.UUID, status: EventStatus | None = None
) -> list[Event]:
    query = select(Event).where(
        Event.photographer_id == photographer_id, Event.deleted_at.is_(None)
    )
    if status:
        query = query.where(Event.status == status)
    return list(await db.scalars(query.order_by(Event.event_date.desc(), Event.id.desc())))


def _apply(event: Event, fields: dict) -> None:
    """Set fields (PIN hashed), then check the rules the DB would otherwise 500 on."""
    pin = fields.pop("pin", None)
    for name, value in fields.items():
        setattr(event, name, value)
    if event.visibility != EventVisibility.PIN_PROTECTED:
        event.access_pin_hash = None  # leaving PIN mode forgets the old PIN
    elif pin:
        event.access_pin_hash = hash_pin(pin)
    elif not event.access_pin_hash:
        raise VedaException("PIN_REQUIRED", "Set a PIN for a PIN-protected gallery", 400)
    if event.starts_at and event.ends_at and event.ends_at < event.starts_at:
        raise VedaException("INVALID_TIMES", "The event can't end before it starts", 400)


async def create(db: AsyncSession, actor: Actor, fields: dict) -> Event:
    # ponytail: no retry on slug collision; 32^10 slugs, the unique index still guarantees it.
    event = Event(photographer_id=actor.id, public_slug=new_event_slug())
    _apply(event, fields)
    db.add(event)
    await db.flush()  # assigns id and server defaults for the audit row
    await db.refresh(event)
    audit.record(db, actor, AuditAction.EVENT_CREATE, "event", event.id, after={"name": event.name})
    return event


async def update(db: AsyncSession, actor: Actor, event_id: int, fields: dict) -> Event:
    event = await get_owned(db, actor.id, event_id, lock=True)
    names = [n if n != "pin" else "access_pin_hash" for n in fields]
    before = {n: getattr(event, n) for n in names}
    _apply(event, dict(fields))
    after = {n: getattr(event, n) for n in names}
    for state in (before, after):  # log that the PIN changed, never the hash
        if "access_pin_hash" in state:
            state["access_pin_hash"] = "set" if state["access_pin_hash"] else None
    audit.record(db, actor, AuditAction.EVENT_UPDATE, "event", event.id, before, after)
    return event


async def set_status(db: AsyncSession, actor: Actor, event_id: int, target: EventStatus) -> Event:
    event = await get_owned(db, actor.id, event_id, lock=True)  # lock: double-clicks serialize
    if event.status not in TRANSITIONS[target]:
        raise VedaException(
            "INVALID_TRANSITION", f"A {event.status} event can't become {target}", 409
        )
    before = event.status
    event.status = target
    if target in _STAMPS:
        setattr(event, _STAMPS[target], datetime.now(UTC))
    audit.record(
        db,
        actor,
        AuditAction.EVENT_STATUS,
        "event",
        event.id,
        {"status": before},
        {"status": target},
    )
    return event


async def new_link(db: AsyncSession, actor: Actor, event_id: int) -> Event:
    """New slug: the old QR and link stop working (e.g. a QR was shared too widely)."""
    event = await get_owned(db, actor.id, event_id, lock=True)
    before = event.public_slug
    event.public_slug = new_event_slug()
    audit.record(
        db, actor, AuditAction.EVENT_NEW_LINK, "event", event.id,
        {"public_slug": before}, {"public_slug": event.public_slug},
    )  # fmt: skip
    return event


async def delete(db: AsyncSession, actor: Actor, event_id: int) -> None:
    # ponytail: soft delete only; its photos keep their storage until Phase 7 adds photo deletion.
    event = await get_owned(db, actor.id, event_id, lock=True)
    event.deleted_at = datetime.now(UTC)
    audit.record(db, actor, AuditAction.EVENT_DELETE, "event", event.id, {"name": event.name})
