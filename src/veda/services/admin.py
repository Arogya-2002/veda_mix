"""Super-admin views and actions. Every change writes an audit log row in the same
transaction. Lock order matches payments: pool → quota → photographer."""

import uuid

from sqlalchemy import func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from veda.constants.enums import (
    AuditAction,
    EventStatus,
    PaymentStatus,
    PhotographerStatus,
    PhotoStatus,
    ProcessingJobStatus,
    SubscriptionStatus,
    UploadFileStatus,
)
from veda.db.models import (
    AuditLog,
    Event,
    Payment,
    Photo,
    Photographer,
    Plan,
    ProcessingJob,
    StoragePool,
    StorageQuota,
    Subscription,
    UploadFile,
)
from veda.exception import VedaException
from veda.integrations import realtime
from veda.services import audit
from veda.services.audit import Actor

# --- photographers -----------------------------------------------------------------------


async def list_photographers(
    db: AsyncSession, status: PhotographerStatus | None, q: str | None, limit: int, offset: int
) -> list[dict]:
    active_sub = (
        select(Subscription.photographer_id, Subscription.expires_at, Plan.name.label("plan"))
        .join(Plan, Plan.id == Subscription.plan_id)
        .where(Subscription.status == SubscriptionStatus.ACTIVE)
        .subquery()
    )
    query = (
        select(Photographer, StorageQuota, active_sub.c.plan, active_sub.c.expires_at)
        .outerjoin(StorageQuota, StorageQuota.photographer_id == Photographer.id)
        .outerjoin(active_sub, active_sub.c.photographer_id == Photographer.id)
    )
    if status:
        query = query.where(Photographer.status == status)
    if q:
        like = f"%{q.strip()}%"
        query = query.where(
            or_(
                Photographer.public_id.ilike(like),
                Photographer.full_name.ilike(like),
                Photographer.studio_name.ilike(like),
                Photographer.email.ilike(like),
                Photographer.phone.ilike(like),
            )
        )
    rows = await db.execute(
        query.order_by(Photographer.created_at.desc()).limit(limit).offset(offset)
    )
    return [
        {
            "photographer": p,
            "quota_bytes": quota.quota_bytes if quota else 0,
            "used_bytes": quota.used_bytes if quota else 0,
            "reserved_bytes": quota.reserved_bytes if quota else 0,
            "plan": plan,
            "subscription_expires_at": expires_at,
        }
        for p, quota, plan, expires_at in rows
    ]


async def _locked_photographer(db: AsyncSession, photographer_id: uuid.UUID) -> Photographer:
    photographer = await db.scalar(
        select(Photographer).where(Photographer.id == photographer_id).with_for_update()
    )
    if photographer is None:
        raise VedaException("PHOTOGRAPHER_NOT_FOUND", "Photographer not found", 404)
    return photographer


async def suspend(db: AsyncSession, actor: Actor, photographer_id: uuid.UUID) -> Photographer:
    """Blocks sign-in to the API and hides their galleries. Reversible."""
    photographer = await _locked_photographer(db, photographer_id)
    if photographer.status in (PhotographerStatus.SUSPENDED, PhotographerStatus.DELETED):
        raise VedaException("INVALID_TRANSITION", f"Photographer is {photographer.status}", 409)
    before = photographer.status
    photographer.status = PhotographerStatus.SUSPENDED
    audit.record(
        db, actor, AuditAction.PHOTOGRAPHER_SUSPEND, "photographer", photographer.id,
        {"status": before}, {"status": photographer.status},
    )  # fmt: skip
    return photographer


async def reactivate(db: AsyncSession, actor: Actor, photographer_id: uuid.UUID) -> Photographer:
    """Back to whatever their billing says: ACTIVE, EXPIRED or PENDING_PAYMENT."""
    photographer = await _locked_photographer(db, photographer_id)
    if photographer.status != PhotographerStatus.SUSPENDED:
        raise VedaException("INVALID_TRANSITION", "Photographer isn't suspended", 409)
    statuses = set(
        await db.scalars(
            select(Subscription.status).where(Subscription.photographer_id == photographer_id)
        )
    )
    if SubscriptionStatus.ACTIVE in statuses:
        photographer.status = PhotographerStatus.ACTIVE
    elif statuses:
        photographer.status = PhotographerStatus.EXPIRED
    else:
        photographer.status = PhotographerStatus.PENDING_PAYMENT
    audit.record(
        db, actor, AuditAction.PHOTOGRAPHER_REACTIVATE, "photographer", photographer.id,
        {"status": PhotographerStatus.SUSPENDED}, {"status": photographer.status},
    )  # fmt: skip
    return photographer


async def _locked_pool(db: AsyncSession) -> StoragePool:
    return (
        await db.scalars(select(StoragePool).where(StoragePool.id == 1).with_for_update())
    ).one()


async def _sold(db: AsyncSession, excluding: uuid.UUID | None = None) -> int:
    query = select(func.coalesce(func.sum(StorageQuota.quota_bytes), 0))
    if excluding:
        query = query.where(StorageQuota.photographer_id != excluding)
    return await db.scalar(query)


async def set_quota(
    db: AsyncSession, actor: Actor, photographer_id: uuid.UUID, quota_bytes: int
) -> StorageQuota:
    """Admin override, e.g. a goodwill top-up. A later payment or expiry sets it again."""
    pool = await _locked_pool(db)  # serializes against other quota changes
    await _locked_photographer(db, photographer_id)
    quota = await db.scalar(
        select(StorageQuota)
        .where(StorageQuota.photographer_id == photographer_id)
        .with_for_update()
    )
    if quota is None:
        quota = StorageQuota(
            photographer_id=photographer_id, quota_bytes=0, used_bytes=0, reserved_bytes=0
        )
        db.add(quota)
    if quota_bytes < quota.used_bytes + quota.reserved_bytes:
        raise VedaException(
            "QUOTA_BELOW_USAGE", "The quota can't be less than what's already stored", 409
        )
    if await _sold(db, excluding=photographer_id) + quota_bytes > pool.capacity_bytes:
        raise VedaException("POOL_FULL", "Not enough unsold space in the storage pool", 409)
    before = quota.quota_bytes
    quota.quota_bytes = quota_bytes
    audit.record(
        db, actor, AuditAction.QUOTA_CHANGE, "photographer", photographer_id,
        {"quota_bytes": before}, {"quota_bytes": quota_bytes},
    )  # fmt: skip
    await db.flush()
    return quota


# --- storage ------------------------------------------------------------------------------


async def storage_overview(db: AsyncSession) -> dict:
    capacity = await db.scalar(select(StoragePool.capacity_bytes).where(StoragePool.id == 1))
    sold, used, reserved, holders = (
        await db.execute(
            select(
                func.coalesce(func.sum(StorageQuota.quota_bytes), 0),
                func.coalesce(func.sum(StorageQuota.used_bytes), 0),
                func.coalesce(func.sum(StorageQuota.reserved_bytes), 0),
                func.count().filter(StorageQuota.quota_bytes > 0),
            )
        )
    ).one()
    return {
        "capacity_bytes": capacity,
        "sold_bytes": sold,  # sum of all quotas; never above capacity
        "unsold_bytes": capacity - sold,
        "used_bytes": used,
        "reserved_bytes": reserved,
        "photographers_with_quota": holders,
    }


async def resize_pool(db: AsyncSession, actor: Actor, capacity_bytes: int) -> dict:
    pool = await _locked_pool(db)
    if capacity_bytes < await _sold(db):
        raise VedaException("POOL_BELOW_SOLD", "Capacity can't drop below the space sold", 409)
    before = pool.capacity_bytes
    pool.capacity_bytes = capacity_bytes
    audit.record(
        db, actor, AuditAction.POOL_RESIZE, "storage_pool", 1,
        {"capacity_bytes": before}, {"capacity_bytes": capacity_bytes},
    )  # fmt: skip
    await db.flush()
    return await storage_overview(db)


# --- payments, events, failures, audit -----------------------------------------------------


async def list_payments(
    db: AsyncSession,
    status: PaymentStatus | None,
    photographer_id: uuid.UUID | None,
    limit: int,
    offset: int,
) -> list[dict]:
    query = (
        select(Payment, Photographer.public_id, Photographer.full_name, Plan.code)
        .join(Photographer, Photographer.id == Payment.photographer_id)
        .join(Plan, Plan.id == Payment.plan_id)
    )
    if status:
        query = query.where(Payment.status == status)
    if photographer_id:
        query = query.where(Payment.photographer_id == photographer_id)
    rows = await db.execute(
        query.order_by(Payment.created_at.desc(), Payment.id.desc()).limit(limit).offset(offset)
    )
    return [
        {"payment": pay, "photographer_public_id": pid, "photographer_name": name, "plan": code}
        for pay, pid, name, code in rows
    ]


async def live_events(db: AsyncSession) -> list[dict]:
    ready = (
        select(Photo.event_id, func.count().label("n"))
        .where(Photo.status == PhotoStatus.READY, Photo.deleted_at.is_(None))
        .group_by(Photo.event_id)
        .subquery()
    )
    rows = (
        await db.execute(
            select(Event, Photographer.public_id, Photographer.studio_name, ready.c.n)
            .join(Photographer, Photographer.id == Event.photographer_id)
            .outerjoin(ready, ready.c.event_id == Event.id)
            .where(Event.status == EventStatus.LIVE, Event.deleted_at.is_(None))
            .order_by(Event.went_live_at.desc())
        )
    ).all()
    viewers = await realtime.live_viewers([e.id for e, *_ in rows])
    return [
        {
            "event": e,
            "photographer_public_id": pid,
            "studio_name": studio,
            "ready_photos": n or 0,
            "live_viewers": viewers.get(e.id) if viewers is not None else None,
        }
        for e, pid, studio, n in rows
    ]


async def failures(db: AsyncSession, limit: int) -> dict:
    """Uploads whose stored file didn't match, and photos the worker couldn't process."""
    uploads = await db.execute(
        select(UploadFile, Photographer.public_id)
        .join(Photographer, Photographer.id == UploadFile.photographer_id)
        .where(UploadFile.status == UploadFileStatus.FAILED)
        .order_by(UploadFile.updated_at.desc())
        .limit(limit)
    )
    photos = await db.execute(
        select(Photo, ProcessingJob, Photographer.public_id)
        .join(ProcessingJob, ProcessingJob.photo_id == Photo.id)
        .join(Photographer, Photographer.id == Photo.photographer_id)
        .where(ProcessingJob.status == ProcessingJobStatus.FAILED)
        .order_by(ProcessingJob.finished_at.desc())
        .limit(limit)
    )
    return {
        "uploads": [{"upload": f, "photographer_public_id": pid} for f, pid in uploads],
        "photos": [{"photo": p, "job": j, "photographer_public_id": pid} for p, j, pid in photos],
    }


async def audit_logs(
    db: AsyncSession,
    entity_type: str | None,
    entity_id: str | None,
    actor_id: uuid.UUID | None,
    limit: int,
    offset: int,
) -> list[AuditLog]:
    query = select(AuditLog)
    if entity_type:
        query = query.where(AuditLog.entity_type == entity_type)
    if entity_id:
        query = query.where(AuditLog.entity_id == entity_id)
    if actor_id:
        query = query.where(AuditLog.actor_id == actor_id)
    return list(
        await db.scalars(
            query.order_by(AuditLog.created_at.desc(), AuditLog.id.desc())
            .limit(limit)
            .offset(offset)
        )
    )
