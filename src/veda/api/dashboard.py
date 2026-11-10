"""Photographer dashboard. Read-only, so expired (read-only) accounts can still use it."""

from datetime import date, datetime
from typing import Annotated

from fastapi import APIRouter, Query
from pydantic import BaseModel

from veda.api.deps import CurrentPhotographer, DbSession
from veda.api.uploads import StorageOut
from veda.config.configuration import local_tz
from veda.constants.enums import UploadFileStatus
from veda.services import dashboard

router = APIRouter(prefix="/dashboard", tags=["dashboard"])


class SubscriptionBrief(BaseModel):
    plan: str
    status: str
    expires_at: datetime


class SummaryOut(BaseModel):
    events: dict[str, int]  # per event status
    photos: dict[str, int]  # per photo status
    failed_uploads: int
    unique_visitors: int  # all galleries, all time
    page_views: int
    downloads: int
    storage: StorageOut
    subscription: SubscriptionBrief | None  # the active one, else the latest


class HourOut(BaseModel):
    hour: int  # 0-23, local time
    photos: int
    bytes: int


class DayOut(BaseModel):
    date: date  # local day
    unique_visitors: int
    page_views: int
    photos_uploaded: int
    downloads: int


class EventStatsOut(BaseModel):
    event_id: int
    photos: dict[str, int]
    total_bytes: int
    unique_visitors: int
    page_views: int
    downloads: dict[str, int]  # WEB / ORIGINAL
    live_viewers: int | None  # guests with the gallery open now; null if unknown
    daily: list[DayOut]


class UploadOut(BaseModel):
    id: int
    event_id: int
    event_name: str
    original_filename: str
    size_bytes: int
    status: str
    attempts: int
    last_error: str | None
    created_at: datetime
    completed_at: datetime | None


@router.get("/summary", response_model=SummaryOut)
async def summary(photographer: CurrentPhotographer, db: DbSession):
    return await dashboard.summary(db, photographer.id)


@router.get("/uploads/hourly", response_model=list[HourOut])
async def hourly(
    photographer: CurrentPhotographer,
    db: DbSession,
    day: Annotated[date | None, Query(alias="date", description="Local; default today")] = None,
    event_id: int | None = None,
):
    day = day or datetime.now(local_tz()).date()
    return await dashboard.hourly_uploads(db, photographer.id, day, event_id)


@router.get("/events/{event_id}/stats", response_model=EventStatsOut)
async def event_stats(event_id: int, photographer: CurrentPhotographer, db: DbSession):
    return await dashboard.event_stats(db, photographer.id, event_id)


@router.get("/upload-history", response_model=list[UploadOut])
async def upload_history(
    photographer: CurrentPhotographer,
    db: DbSession,
    status: UploadFileStatus | None = None,
    event_id: int | None = None,
    limit: Annotated[int, Query(ge=1, le=200)] = 50,
    offset: Annotated[int, Query(ge=0)] = 0,
):
    rows = await dashboard.upload_history(db, photographer.id, status, event_id, limit, offset)
    return [
        UploadOut(
            **{k: getattr(r["upload"], k) for k in UploadOut.model_fields if k != "event_name"},
            event_name=r["event_name"],
        )
        for r in rows
    ]
