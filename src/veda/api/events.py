import io
from datetime import date, datetime
from typing import Literal

import segno
from fastapi import APIRouter, Request, Response
from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, computed_field

from veda.api.deps import ActivePhotographer, CurrentPhotographer, DbSession
from veda.config.configuration import get_settings
from veda.constants.enums import ActorType, DownloadQuality, EventStatus, EventVisibility
from veda.db.models import Photographer
from veda.services import events
from veda.services.audit import Actor

router = APIRouter(tags=["events"])

_PIN = Field(default=None, pattern=r"^\d{4,8}$", description="4-8 digits; stored hashed")


class EventIn(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True, extra="forbid")

    name: str = Field(min_length=1, max_length=160)
    event_type: str | None = Field(default=None, max_length=40)
    event_date: date
    starts_at: AwareDatetime | None = None
    ends_at: AwareDatetime | None = None
    venue: str | None = Field(default=None, max_length=255)
    visibility: EventVisibility = EventVisibility.PUBLIC
    pin: str | None = _PIN
    allow_download: bool = True
    download_quality: DownloadQuality = DownloadQuality.WEB


class EventPatch(BaseModel):
    """Only sent fields change. Required columns default to None but reject an explicit null."""

    model_config = ConfigDict(str_strip_whitespace=True, extra="forbid")

    name: str = Field(default=None, min_length=1, max_length=160)
    event_type: str | None = Field(default=None, max_length=40)
    event_date: date = None
    starts_at: AwareDatetime | None = None
    ends_at: AwareDatetime | None = None
    venue: str | None = Field(default=None, max_length=255)
    visibility: EventVisibility = None
    pin: str | None = _PIN
    allow_download: bool = None
    download_quality: DownloadQuality = None


class EventOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    name: str
    event_type: str | None
    event_date: date
    starts_at: datetime | None
    ends_at: datetime | None
    venue: str | None
    status: str
    public_slug: str
    visibility: str
    allow_download: bool
    download_quality: str
    went_live_at: datetime | None
    completed_at: datetime | None
    created_at: datetime
    access_pin_hash: str | None = Field(exclude=True)

    @computed_field
    def pin_set(self) -> bool:
        return self.access_pin_hash is not None

    @computed_field
    def gallery_url(self) -> str:
        return gallery_url(self.public_slug)


def gallery_url(slug: str) -> str:
    return f"{get_settings().gallery_base_url.rstrip('/')}/{slug}"


def _actor(photographer: Photographer, request: Request) -> Actor:
    return Actor.from_request(ActorType.PHOTOGRAPHER, photographer.id, request)


@router.get("/events", response_model=list[EventOut])
async def list_events(
    photographer: CurrentPhotographer, db: DbSession, status: EventStatus | None = None
):
    return await events.list_owned(db, photographer.id, status)


@router.post("/events", status_code=201, response_model=EventOut)
async def create_event(
    body: EventIn, photographer: ActivePhotographer, request: Request, db: DbSession
):
    event = await events.create(db, _actor(photographer, request), body.model_dump())
    await db.commit()
    return event


@router.get("/events/{event_id}", response_model=EventOut)
async def get_event(event_id: int, photographer: CurrentPhotographer, db: DbSession):
    return await events.get_owned(db, photographer.id, event_id)


@router.patch("/events/{event_id}", response_model=EventOut)
async def update_event(
    event_id: int, body: EventPatch, photographer: ActivePhotographer, request: Request,
    db: DbSession,
):  # fmt: skip
    actor = _actor(photographer, request)
    event = await events.update(db, actor, event_id, body.model_dump(exclude_unset=True))
    await db.commit()
    return event


@router.delete("/events/{event_id}", status_code=204)
async def delete_event(
    event_id: int, photographer: CurrentPhotographer, request: Request, db: DbSession
):
    # Allowed after expiry too: people can always remove their own data.
    await events.delete(db, _actor(photographer, request), event_id)
    await db.commit()


async def _set_status(event_id, photographer, request, db, target):
    event = await events.set_status(db, _actor(photographer, request), event_id, target)
    await db.commit()
    return event


@router.post("/events/{event_id}/go-live", response_model=EventOut)
async def go_live(event_id: int, photographer: ActivePhotographer, request: Request, db: DbSession):
    return await _set_status(event_id, photographer, request, db, EventStatus.LIVE)


# Completing and archiving only wind things down, so they work after expiry too.
@router.post("/events/{event_id}/complete", response_model=EventOut)
async def complete(
    event_id: int, photographer: CurrentPhotographer, request: Request, db: DbSession
):
    return await _set_status(event_id, photographer, request, db, EventStatus.COMPLETED)


@router.post("/events/{event_id}/archive", response_model=EventOut)
async def archive(
    event_id: int, photographer: CurrentPhotographer, request: Request, db: DbSession
):
    return await _set_status(event_id, photographer, request, db, EventStatus.ARCHIVED)


_QR_TYPES = {"png": "image/png", "svg": "image/svg+xml"}


@router.get("/events/{event_id}/qr")
async def event_qr(
    event_id: int, photographer: CurrentPhotographer, db: DbSession,
    format: Literal["png", "svg"] = "png",
):  # fmt: skip
    """Drawn on request (about a millisecond), so it always matches the current link."""
    event = await events.get_owned(db, photographer.id, event_id)
    buffer = io.BytesIO()
    # error="q": still scans with ~25% of the code damaged or covered (printed cards, logos).
    segno.make(gallery_url(event.public_slug), error="q").save(
        buffer, kind=format, scale=10, border=4
    )
    return Response(
        buffer.getvalue(),
        media_type=_QR_TYPES[format],
        headers={"Content-Disposition": f'attachment; filename="{event.public_slug}.{format}"'},
    )


@router.post("/events/{event_id}/qr/regenerate", response_model=EventOut)
async def regenerate_qr(
    event_id: int, photographer: ActivePhotographer, request: Request, db: DbSession
):
    event = await events.new_link(db, _actor(photographer, request), event_id)
    await db.commit()
    return event
