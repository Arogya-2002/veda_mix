"""Photographer dashboard: read-only numbers, every query scoped to the caller.

Days and hours are local (config timezone); the database stores UTC.
"""

import uuid
from datetime import date, datetime, time, timedelta

from sqlalchemy import Date, cast, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from veda.config.configuration import local_tz
from veda.constants.enums import (
    EventStatus,
    PhotoStatus,
    SubscriptionStatus,
    UploadFileStatus,
)
from veda.db.models import (
    Event,
    EventDailyStat,
    GalleryVisitor,
    Photo,
    PhotoDownload,
    Plan,
    Subscription,
    UploadFile,
)
from veda.integrations import realtime
from veda.services import events, uploads


def _counts(rows, keys) -> dict[str, int]:
    return {k.value: 0 for k in keys} | {status: n for status, n in rows}


def _local_day(column):
    return cast(func.timezone(local_tz().key, column), Date)


async def summary(db: AsyncSession, photographer_id: uuid.UUID) -> dict:
    owned = (Event.photographer_id == photographer_id, Event.deleted_at.is_(None))
    event_counts = await db.execute(
        select(Event.status, func.count()).where(*owned).group_by(Event.status)
    )
    photo_counts = await db.execute(
        select(Photo.status, func.count())
        .where(Photo.photographer_id == photographer_id, Photo.deleted_at.is_(None))
        .group_by(Photo.status)
    )
    failed_uploads = await db.scalar(
        select(func.count()).where(
            UploadFile.photographer_id == photographer_id,
            UploadFile.status == UploadFileStatus.FAILED,
        )
    )
    visitors, views = (
        await db.execute(
            select(
                func.count(GalleryVisitor.id), func.coalesce(func.sum(GalleryVisitor.page_views), 0)
            )
            .join(Event, Event.id == GalleryVisitor.event_id)
            .where(*owned)
        )
    ).one()
    downloads = await db.scalar(
        select(func.count()).select_from(PhotoDownload).join(Event).where(*owned)
    )
    subscription = (
        await db.execute(
            select(Subscription, Plan)
            .join(Plan, Plan.id == Subscription.plan_id)
            .where(Subscription.photographer_id == photographer_id)
            .order_by(
                (Subscription.status == SubscriptionStatus.ACTIVE).desc(),
                Subscription.expires_at.desc(),
            )
            .limit(1)
        )
    ).one_or_none()
    return {
        "events": _counts(event_counts, EventStatus),
        "photos": _counts(photo_counts, PhotoStatus),
        "failed_uploads": failed_uploads,  # can be retried; space stays reserved meanwhile
        "unique_visitors": visitors,
        "page_views": views,
        "downloads": downloads,
        "storage": await uploads.storage_summary(db, photographer_id),
        "subscription": (
            {
                "plan": subscription.Plan.name,
                "status": subscription.Subscription.status,
                "expires_at": subscription.Subscription.expires_at,
            }
            if subscription
            else None
        ),
    }


async def hourly_uploads(
    db: AsyncSession, photographer_id: uuid.UUID, day: date, event_id: int | None
) -> list[dict]:
    """24 rows: photos uploaded (and their bytes) in each local hour of `day`."""
    tz = local_tz()
    start = datetime.combine(day, time(), tz)
    hour = func.extract("hour", func.timezone(tz.key, Photo.uploaded_at))
    query = select(hour, func.count(), func.sum(Photo.size_bytes)).where(
        Photo.photographer_id == photographer_id,
        Photo.deleted_at.is_(None),
        Photo.uploaded_at >= start,
        Photo.uploaded_at < start + timedelta(days=1),
    )
    if event_id is not None:
        query = query.where(Photo.event_id == event_id)
    found = {int(h): (n, b) for h, n, b in await db.execute(query.group_by(hour))}
    return [
        {"hour": h, "photos": found.get(h, (0, 0))[0], "bytes": found.get(h, (0, 0))[1]}
        for h in range(24)
    ]


async def event_stats(db: AsyncSession, photographer_id: uuid.UUID, event_id: int) -> dict:
    event = await events.get_owned(db, photographer_id, event_id)
    photo_rows = await db.execute(
        select(Photo.status, func.count(), func.coalesce(func.sum(Photo.size_bytes), 0))
        .where(Photo.event_id == event.id, Photo.deleted_at.is_(None))
        .group_by(Photo.status)
    )
    photos, total_bytes = {s.value: 0 for s in PhotoStatus}, 0
    for status, n, size in photo_rows:
        photos[status], total_bytes = n, total_bytes + size

    visitors, views = (
        await db.execute(
            select(
                func.count(GalleryVisitor.id), func.coalesce(func.sum(GalleryVisitor.page_views), 0)
            ).where(GalleryVisitor.event_id == event.id)
        )
    ).one()
    download_rows = await db.execute(
        select(PhotoDownload.quality, func.count())
        .where(PhotoDownload.event_id == event.id)
        .group_by(PhotoDownload.quality)
    )
    live = await realtime.live_viewers([event.id])

    # Day by day: traffic from the counters, photos and downloads from their own rows.
    days: dict[date, dict] = {}

    def day(d: date) -> dict:
        return days.setdefault(
            d,
            {
                "date": d,
                "unique_visitors": 0,
                "page_views": 0,
                "photos_uploaded": 0,
                "downloads": 0,
            },
        )

    for stat in await db.scalars(select(EventDailyStat).where(EventDailyStat.event_id == event.id)):
        day(stat.stat_date).update(unique_visitors=stat.unique_visitors, page_views=stat.page_views)
    uploaded_on = _local_day(Photo.uploaded_at)
    for d, n in await db.execute(
        select(uploaded_on, func.count())
        .where(Photo.event_id == event.id, Photo.deleted_at.is_(None))
        .group_by(uploaded_on)
    ):
        day(d)["photos_uploaded"] = n
    downloaded_on = _local_day(PhotoDownload.created_at)
    for d, n in await db.execute(
        select(downloaded_on, func.count())
        .where(PhotoDownload.event_id == event.id)
        .group_by(downloaded_on)
    ):
        day(d)["downloads"] = n

    return {
        "event_id": event.id,
        "photos": photos,
        "total_bytes": total_bytes,
        "unique_visitors": visitors,
        "page_views": views,
        "downloads": {"WEB": 0, "ORIGINAL": 0} | dict(download_rows.all()),
        "live_viewers": live.get(event.id) if live is not None else None,
        "daily": [days[d] for d in sorted(days)],
    }


async def upload_history(
    db: AsyncSession,
    photographer_id: uuid.UUID,
    status: UploadFileStatus | None,
    event_id: int | None,
    limit: int,
    offset: int,
) -> list[dict]:
    query = (
        select(UploadFile, Event.name)
        .join(Event, Event.id == UploadFile.event_id)
        .where(UploadFile.photographer_id == photographer_id)
    )
    if status:
        query = query.where(UploadFile.status == status)
    if event_id is not None:
        query = query.where(UploadFile.event_id == event_id)
    rows = await db.execute(
        query.order_by(UploadFile.created_at.desc(), UploadFile.id.desc())
        .limit(limit)
        .offset(offset)
    )
    return [{"upload": f, "event_name": name} for f, name in rows]
