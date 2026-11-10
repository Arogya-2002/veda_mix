"""What guests see. Guests never log in and never handle internal ids: a gallery is found by
its random slug, a photo by its random public_id.

Who may look:
- the event isn't deleted, ARCHIVED or PRIVATE, and its photographer isn't suspended/deleted
  (an expired subscription stops uploads, not viewing);
- PIN_PROTECTED events also need an unlock token, issued for the right PIN.
Only READY photos are shown, newest first.
"""

import base64
import binascii
import hashlib
import hmac
import time
from datetime import datetime

from fastapi.concurrency import run_in_threadpool
from redis import RedisError
from sqlalchemy import func, select, tuple_
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from veda.config.configuration import get_params
from veda.constants.enums import (
    DownloadQuality,
    EventStatus,
    EventVisibility,
    PhotographerStatus,
    PhotoStatus,
    PhotoVariantName,
)
from veda.db.models import (
    Event,
    GalleryVisitor,
    Photo,
    PhotoDownload,
    Photographer,
    PhotoVariant,
)
from veda.exception import VedaException
from veda.integrations import storage
from veda.services import rate_limit
from veda.services.photos import live_fields
from veda.utils.common import verify_pin

OPEN_TO_GUESTS = (EventStatus.UPCOMING, EventStatus.LIVE, EventStatus.COMPLETED)
OWNER_HIDDEN = (PhotographerStatus.SUSPENDED, PhotographerStatus.DELETED)


def _p() -> dict:
    return get_params()["gallery"]


def _check_open(event: Event, owner: Photographer) -> None:
    if (
        event.status not in OPEN_TO_GUESTS
        or event.visibility == EventVisibility.PRIVATE
        or owner.status in OWNER_HIDDEN
        or owner.deleted_at is not None
    ):
        raise VedaException("GALLERY_UNAVAILABLE", "This gallery isn't open to guests", 403)


async def find(db: AsyncSession, slug: str) -> tuple[Event, Photographer]:
    row = (
        await db.execute(
            select(Event, Photographer)
            .join(Photographer, Photographer.id == Event.photographer_id)
            .where(Event.public_slug == slug, Event.deleted_at.is_(None))
        )
    ).one_or_none()
    if row is None:
        raise VedaException("GALLERY_NOT_FOUND", "Gallery not found", 404)
    event, owner = row
    _check_open(event, owner)
    return event, owner


# --- PIN unlock ----------------------------------------------------------------------
# Token = "<expiry>.<HMAC(expiry)>", keyed with the event's PIN hash: a secret only the
# server has, so no extra key to manage, and changing the PIN logs every guest out.


def unlock_cookie(event_id: int) -> str:
    return f"veda_gallery_{event_id}"


def _signature(event: Event, expires: int) -> str:
    key = (event.access_pin_hash or "").encode()
    return hmac.new(key, f"{event.id}.{expires}".encode(), hashlib.sha256).hexdigest()


def is_unlocked(event: Event, token: str | None) -> bool:
    if event.visibility != EventVisibility.PIN_PROTECTED:
        return True
    expires, _, signature = (token or "").partition(".")
    if not expires.isdigit() or int(expires) < time.time() or not event.access_pin_hash:
        return False
    return hmac.compare_digest(signature, _signature(event, int(expires)))


def require_unlocked(event: Event, token: str | None) -> None:
    if not is_unlocked(event, token):
        raise VedaException("PIN_REQUIRED", "Enter the event PIN to see this gallery", 401)


async def unlock(event: Event, pin: str, ip: str) -> str:
    """A token for the unlock cookie. Wrong guesses are limited per IP and per event."""
    if event.visibility != EventVisibility.PIN_PROTECTED:
        raise VedaException("NO_PIN", "This gallery has no PIN", 409)
    p = _p()
    limits = {
        f"pin:{event.id}:{ip}": p["unlock_failures_per_ip"],
        f"pin:{event.id}": p["unlock_failures_per_event"],
    }
    try:  # fail closed: without Redis there's no limit, so no guessing either
        if await rate_limit.over_limit(limits):
            raise VedaException("TOO_MANY_ATTEMPTS", "Too many wrong PINs; try again later", 429)
        if not await run_in_threadpool(verify_pin, pin, event.access_pin_hash):  # scrypt: slow
            await rate_limit.hit(list(limits), p["unlock_window_minutes"] * 60)
            raise VedaException("WRONG_PIN", "That PIN isn't right", 401)
    except RedisError as e:
        raise VedaException("UNAVAILABLE", "Please try again in a minute", 503) from e
    expires = int(time.time()) + p["unlock_ttl_hours"] * 3600
    return f"{expires}.{_signature(event, expires)}"


# --- photos ----------------------------------------------------------------------------


def guest_photo(fields: dict) -> dict:
    """Stored/published photo fields -> what a guest gets: fresh short-lived image links."""
    ttl = _p()["signed_url_ttl_minutes"] * 60
    shown = ("public_id", "width", "height", "taken_at", "uploaded_at")
    return {k: fields[k] for k in shown} | {
        "thumb_url": storage.presign_get(fields["thumb_key"], ttl),
        "web_url": storage.presign_get(fields["web_key"], ttl),
    }


def _visible(event_id: int):
    return (
        Photo.event_id == event_id,
        Photo.status == PhotoStatus.READY,
        Photo.deleted_at.is_(None),
    )


def _encode_cursor(photo: Photo) -> str:
    raw = f"{photo.uploaded_at.isoformat()}|{photo.id}".encode()
    return base64.urlsafe_b64encode(raw).decode().rstrip("=")


def _decode_cursor(cursor: str) -> tuple[datetime, int]:
    try:
        raw = base64.urlsafe_b64decode(cursor + "=" * (-len(cursor) % 4)).decode()
        uploaded_at, _, photo_id = raw.rpartition("|")
        when = datetime.fromisoformat(uploaded_at)
        if when.tzinfo is None:
            raise ValueError("naive")
        return when, int(photo_id)
    except (ValueError, binascii.Error, UnicodeDecodeError) as e:
        raise VedaException("INVALID_CURSOR", "Invalid page cursor", 400) from e


async def photo_page(db: AsyncSession, event_id: int, cursor: str | None) -> dict:
    """{"photos": [...], "next_cursor": str | None}, newest first. Keyset paging on
    (uploaded_at, id): stable while new photos arrive (those come via the live stream)."""
    size = _p()["page_size"]
    query = select(Photo).where(*_visible(event_id))
    if cursor:
        query = query.where(tuple_(Photo.uploaded_at, Photo.id) < tuple_(*_decode_cursor(cursor)))
    photos = list(
        await db.scalars(query.order_by(Photo.uploaded_at.desc(), Photo.id.desc()).limit(size + 1))
    )
    more, photos = len(photos) > size, photos[:size]

    keys: dict[int, dict[str, str]] = {p.id: {} for p in photos}
    rows = await db.execute(
        select(PhotoVariant.photo_id, PhotoVariant.variant, PhotoVariant.s3_key).where(
            PhotoVariant.photo_id.in_(keys)
        )
    )
    for photo_id, variant, key in rows:
        keys[photo_id][variant] = key
    return {
        "photos": [guest_photo(live_fields(p, keys[p.id])) for p in photos],
        "next_cursor": _encode_cursor(photos[-1]) if more else None,
    }


async def photo_count(db: AsyncSession, event_id: int) -> int:
    return await db.scalar(select(func.count()).select_from(Photo).where(*_visible(event_id)))


# --- visitors and downloads ----------------------------------------------------------------


def _token_hash(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


async def record_visit(db: AsyncSession, event_id: int, visitor_token: str) -> None:
    """One row per browser per event; repeat visits bump page_views."""
    await db.execute(
        insert(GalleryVisitor)
        .values(event_id=event_id, visitor_token_hash=_token_hash(visitor_token))
        .on_conflict_do_update(
            index_elements=["event_id", "visitor_token_hash"],
            set_={"last_seen_at": func.now(), "page_views": GalleryVisitor.page_views + 1},
        )
    )


async def download(
    db: AsyncSession, public_id: str, unlock_tokens: dict[str, str], visitor_token: str | None
) -> str:
    """Log the download and return a link that saves the file. The event decides the
    quality: the web copy (default) or the untouched original."""
    row = (
        await db.execute(
            select(Photo, Event, Photographer)
            .join(Event, Event.id == Photo.event_id)
            .join(Photographer, Photographer.id == Event.photographer_id)
            .where(
                Photo.public_id == public_id,
                Photo.status == PhotoStatus.READY,
                Photo.deleted_at.is_(None),
                Event.deleted_at.is_(None),
            )
        )
    ).one_or_none()
    if row is None:
        raise VedaException("PHOTO_NOT_FOUND", "Photo not found", 404)
    photo, event, owner = row
    _check_open(event, owner)
    require_unlocked(event, unlock_tokens.get(unlock_cookie(event.id)))
    if not event.allow_download:
        raise VedaException("DOWNLOAD_DISABLED", "Downloads are turned off for this event", 403)

    if event.download_quality == DownloadQuality.ORIGINAL:
        key, filename = photo.s3_key, photo.original_filename
    else:
        key = await db.scalar(
            select(PhotoVariant.s3_key).where(
                PhotoVariant.photo_id == photo.id, PhotoVariant.variant == PhotoVariantName.WEB
            )
        )
        filename = photo.original_filename.rsplit(".", 1)[0] + ".webp"

    visitor_id = None
    if visitor_token:
        visitor_id = await db.scalar(
            select(GalleryVisitor.id).where(
                GalleryVisitor.event_id == event.id,
                GalleryVisitor.visitor_token_hash == _token_hash(visitor_token),
            )
        )
    db.add(
        PhotoDownload(
            event_id=event.id,
            photo_id=photo.id,
            visitor_id=visitor_id,
            quality=event.download_quality,
        )
    )
    return storage.presign_get(key, _p()["signed_url_ttl_minutes"] * 60, download_as=filename)
