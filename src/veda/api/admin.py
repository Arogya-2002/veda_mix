"""Super-admin. Sign-in is Supabase (email + password + TOTP); CurrentAdmin requires an
admins row and an MFA-verified (aal2) token. There is no admin sign-up route."""

import uuid
from datetime import datetime
from typing import Annotated

from fastapi import APIRouter, Query, Request
from pydantic import BaseModel, ConfigDict, Field, IPvAnyAddress

from veda.api.deps import CurrentAdmin, DbSession
from veda.constants.enums import ActorType, PaymentStatus, PhotographerStatus
from veda.services import admin
from veda.services.audit import Actor

router = APIRouter(prefix="/admin", tags=["admin"])

Limit = Annotated[int, Query(ge=1, le=200)]
Offset = Annotated[int, Query(ge=0)]


def _actor(user, request: Request) -> Actor:
    return Actor.from_request(ActorType.ADMIN, user.id, request)


def _flat(schema: type[BaseModel], row: dict, key: str) -> BaseModel:
    """schema from row[key]'s attributes plus the row's other (joined-in) fields."""
    extra = {k: v for k, v in row.items() if k != key}
    obj = row[key]
    return schema(**{k: getattr(obj, k) for k in schema.model_fields if k not in extra}, **extra)


class _Orm(BaseModel):
    model_config = ConfigDict(from_attributes=True)


class PhotographerRow(BaseModel):
    id: uuid.UUID
    public_id: str
    full_name: str
    studio_name: str | None
    email: str
    phone: str
    city: str | None
    status: str
    created_at: datetime
    plan: str | None  # active subscription's plan
    subscription_expires_at: datetime | None
    quota_bytes: int
    used_bytes: int
    reserved_bytes: int


class PhotographerStatusOut(_Orm):
    id: uuid.UUID
    public_id: str
    status: str


class QuotaIn(BaseModel):
    quota_bytes: int = Field(ge=0)


class QuotaOut(_Orm):
    photographer_id: uuid.UUID
    quota_bytes: int
    used_bytes: int
    reserved_bytes: int


class PoolIn(BaseModel):
    capacity_bytes: int = Field(gt=0)


class StorageOverview(BaseModel):
    capacity_bytes: int
    sold_bytes: int
    unsold_bytes: int
    used_bytes: int
    reserved_bytes: int
    photographers_with_quota: int


class PaymentRow(BaseModel):
    id: int
    photographer_public_id: str
    photographer_name: str
    plan: str
    amount_paise: int
    currency: str
    status: str
    method: str | None
    gateway_order_id: str
    gateway_payment_id: str | None
    failure_reason: str | None
    paid_at: datetime | None
    created_at: datetime


class LiveEventRow(BaseModel):
    id: int
    name: str
    public_slug: str
    went_live_at: datetime | None
    photographer_public_id: str
    studio_name: str | None
    ready_photos: int
    live_viewers: int | None


class FailedUpload(BaseModel):
    id: int
    photographer_public_id: str
    event_id: int
    original_filename: str
    size_bytes: int
    attempts: int
    last_error: str | None
    updated_at: datetime


class FailedPhoto(BaseModel):
    public_id: str
    photographer_public_id: str
    event_id: int
    original_filename: str
    attempts: int
    last_error: str | None
    failed_at: datetime | None


class FailuresOut(BaseModel):
    uploads: list[FailedUpload]
    photos: list[FailedPhoto]


class AuditRow(_Orm):
    id: int
    actor_type: str
    actor_id: uuid.UUID | None
    action: str
    entity_type: str
    entity_id: str
    before_state: dict | None
    after_state: dict | None
    ip_address: IPvAnyAddress | None
    created_at: datetime


# --- photographers -------------------------------------------------------------------------


@router.get("/photographers", response_model=list[PhotographerRow])
async def photographers(
    _: CurrentAdmin,
    db: DbSession,
    status: PhotographerStatus | None = None,
    q: str | None = Query(default=None, description="ID, name, studio, email or phone"),
    limit: Limit = 50,
    offset: Offset = 0,
):
    rows = await admin.list_photographers(db, status, q, limit, offset)
    return [_flat(PhotographerRow, r, "photographer") for r in rows]


@router.post("/photographers/{photographer_id}/suspend", response_model=PhotographerStatusOut)
async def suspend(photographer_id: uuid.UUID, user: CurrentAdmin, request: Request, db: DbSession):
    photographer = await admin.suspend(db, _actor(user, request), photographer_id)
    await db.commit()
    return photographer


@router.post("/photographers/{photographer_id}/reactivate", response_model=PhotographerStatusOut)
async def reactivate(
    photographer_id: uuid.UUID, user: CurrentAdmin, request: Request, db: DbSession
):
    photographer = await admin.reactivate(db, _actor(user, request), photographer_id)
    await db.commit()
    return photographer


@router.patch("/photographers/{photographer_id}/quota", response_model=QuotaOut)
async def set_quota(
    photographer_id: uuid.UUID, body: QuotaIn, user: CurrentAdmin, request: Request, db: DbSession
):
    quota = await admin.set_quota(db, _actor(user, request), photographer_id, body.quota_bytes)
    await db.commit()
    return quota


# --- storage, payments, events, failures, audit ---------------------------------------------


@router.get("/storage", response_model=StorageOverview)
async def storage(_: CurrentAdmin, db: DbSession):
    return await admin.storage_overview(db)


@router.patch("/storage/pool", response_model=StorageOverview)
async def resize_pool(body: PoolIn, user: CurrentAdmin, request: Request, db: DbSession):
    overview = await admin.resize_pool(db, _actor(user, request), body.capacity_bytes)
    await db.commit()
    return overview


@router.get("/payments", response_model=list[PaymentRow])
async def payments(
    _: CurrentAdmin,
    db: DbSession,
    status: PaymentStatus | None = None,
    photographer_id: uuid.UUID | None = None,
    limit: Limit = 50,
    offset: Offset = 0,
):
    rows = await admin.list_payments(db, status, photographer_id, limit, offset)
    return [_flat(PaymentRow, r, "payment") for r in rows]


@router.get("/events/live", response_model=list[LiveEventRow])
async def live_events(_: CurrentAdmin, db: DbSession):
    return [_flat(LiveEventRow, r, "event") for r in await admin.live_events(db)]


@router.get("/uploads/failed", response_model=FailuresOut)
async def failed(_: CurrentAdmin, db: DbSession, limit: Limit = 50):
    found = await admin.failures(db, limit)
    return FailuresOut(
        uploads=[_flat(FailedUpload, r, "upload") for r in found["uploads"]],
        photos=[
            FailedPhoto(
                public_id=r["photo"].public_id,
                photographer_public_id=r["photographer_public_id"],
                event_id=r["photo"].event_id,
                original_filename=r["photo"].original_filename,
                attempts=r["job"].attempts,
                last_error=r["job"].last_error,
                failed_at=r["job"].finished_at,
            )
            for r in found["photos"]
        ],
    )


@router.get("/audit-logs", response_model=list[AuditRow])
async def audit_logs(
    _: CurrentAdmin,
    db: DbSession,
    entity_type: str | None = None,
    entity_id: str | None = None,
    actor_id: uuid.UUID | None = None,
    limit: Limit = 50,
    offset: Offset = 0,
):
    return await admin.audit_logs(db, entity_type, entity_id, actor_id, limit, offset)
