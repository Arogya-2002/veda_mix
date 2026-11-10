"""Public guest gallery: no login. Reached from the event's QR code (/g/{slug} in React).

Browsers carry two httpOnly cookies: an anonymous visitor id (unique-guest counts) and,
for PIN-protected events, the unlock token. Cookies (not headers) because EventSource
can't send headers.
"""

import secrets
from datetime import date, datetime

from fastapi import APIRouter, Request, Response
from fastapi.responses import RedirectResponse
from pydantic import BaseModel, Field
from redis import RedisError
from sse_starlette import EventSourceResponse

from veda.api.deps import DbSession
from veda.config.configuration import get_params, get_settings
from veda.db import SessionLocal
from veda.exception import VedaException
from veda.integrations import realtime
from veda.logging import get_logger
from veda.services import gallery

router = APIRouter(prefix="/public", tags=["gallery"])
log = get_logger(__name__)

VISITOR_COOKIE = "veda_visitor"


class GalleryPhoto(BaseModel):
    public_id: str
    width: int | None
    height: int | None
    taken_at: datetime | None  # camera's local clock
    uploaded_at: datetime
    thumb_url: str  # short-lived (signed_url_ttl_minutes); refetch the page when expired
    web_url: str


class PhotoPage(BaseModel):
    photos: list[GalleryPhoto]
    next_cursor: str | None  # pass to /photos?cursor= for older photos; null = no more


class GalleryOut(BaseModel):
    slug: str
    name: str
    event_type: str | None
    event_date: date
    venue: str | None
    status: str
    studio_name: str | None
    locked: bool  # true: show the PIN screen, POST /unlock, then reload
    allow_download: bool
    photo_count: int | None  # null while locked
    page: PhotoPage | None  # first page, newest first; null while locked


class UnlockIn(BaseModel):
    pin: str = Field(pattern=r"^\d{4,8}$")


def _set_cookie(response: Response, name: str, value: str, max_age_s: int) -> None:
    response.set_cookie(
        name,
        value,
        max_age=max_age_s,
        httponly=True,
        samesite="lax",
        secure=get_settings().secure_cookies,
    )


def _unlock_token(request: Request, event_id: int) -> str | None:
    return request.cookies.get(gallery.unlock_cookie(event_id))


@router.get("/galleries/{slug}", response_model=GalleryOut)
async def get_gallery(slug: str, request: Request, response: Response, db: DbSession):
    event, owner = await gallery.find(db, slug)
    visitor = request.cookies.get(VISITOR_COOKIE)
    if not visitor or len(visitor) > 64:
        visitor = secrets.token_urlsafe(24)
        days = get_params()["gallery"]["visitor_cookie_days"]
        _set_cookie(response, VISITOR_COOKIE, visitor, days * 86400)
    await gallery.record_visit(db, event.id, visitor)

    locked = not gallery.is_unlocked(event, _unlock_token(request, event.id))
    page = None if locked else await gallery.photo_page(db, event.id, None)
    count = None if locked else await gallery.photo_count(db, event.id)
    await db.commit()
    return GalleryOut(
        slug=event.public_slug,
        name=event.name,
        event_type=event.event_type,
        event_date=event.event_date,
        venue=event.venue,
        status=event.status,
        studio_name=owner.studio_name,
        locked=locked,
        allow_download=event.allow_download,
        photo_count=count,
        page=page,
    )


@router.post("/galleries/{slug}/unlock", status_code=204)
async def unlock(slug: str, body: UnlockIn, request: Request, response: Response, db: DbSession):
    event, _ = await gallery.find(db, slug)
    # Behind a load balancer, run uvicorn with --proxy-headers so this is the guest's IP.
    ip = request.client.host if request.client else "unknown"
    token = await gallery.unlock(event, body.pin, ip)
    hours = get_params()["gallery"]["unlock_ttl_hours"]
    _set_cookie(response, gallery.unlock_cookie(event.id), token, hours * 3600)


@router.get("/galleries/{slug}/photos", response_model=PhotoPage)
async def photos(slug: str, request: Request, db: DbSession, cursor: str | None = None):
    event, _ = await gallery.find(db, slug)
    gallery.require_unlocked(event, _unlock_token(request, event.id))
    return await gallery.photo_page(db, event.id, cursor)


@router.get("/galleries/{slug}/stream")
async def stream(slug: str, request: Request):
    """Server-Sent Events. `ready` once connected (refetch the first page to catch anything
    missed while offline), then `photo` with a GalleryPhoto each time one is processed.
    503 if live updates are down: fall back to polling the first page."""
    # Own short session: a stream can stay open for hours and mustn't hold a DB connection.
    async with SessionLocal() as db:
        event, _ = await gallery.find(db, slug)
    gallery.require_unlocked(event, _unlock_token(request, event.id))

    updates = realtime.photo_events(event.id)
    try:
        await anext(updates)  # subscribe now, so a Redis outage is a clean 503
    except RedisError as e:
        raise VedaException("LIVE_UNAVAILABLE", "Live updates are unavailable", 503) from e

    async def events():
        try:
            yield {"event": "ready", "data": ""}
            async for update in updates:
                photo = GalleryPhoto(**gallery.guest_photo(update["photo"]))
                yield {"event": "photo", "data": photo.model_dump_json()}
        finally:
            await updates.aclose()

    return EventSourceResponse(events(), ping=15)


@router.get("/photos/{public_id}/download", status_code=302)
async def download(public_id: str, request: Request, db: DbSession):
    """Redirects to a short-lived link that saves the file (use it as a plain <a href>)."""
    url = await gallery.download(
        db, public_id, request.cookies, request.cookies.get(VISITOR_COOKIE)
    )
    await db.commit()
    return RedirectResponse(url, status_code=302)
