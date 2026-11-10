"""Reserve -> upload straight to S3 -> verify -> commit.

Quota invariant: used_bytes + reserved_bytes <= quota_bytes, guarded by locking the
photographer's storage_quotas row. The pool needs no lock: Phase 2 never sells more
quota than the pool holds. Lock order: upload_files row, then storage_quotas row.
"""

import uuid
from datetime import UTC, datetime, timedelta

from fastapi.concurrency import run_in_threadpool
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import Session

from veda.config.configuration import get_params
from veda.constants.enums import (
    EventStatus,
    ProcessingJobType,
    UploadDecision,
    UploadFileStatus,
    UploadSource,
)
from veda.db.models import (
    Event,
    Photo,
    Photographer,
    ProcessingJob,
    StorageQuota,
    UploadFile,
    UploadSession,
)
from veda.exception import VedaException
from veda.integrations import storage
from veda.services import events
from veda.utils.common import new_photo_public_id

OPEN_EVENT = (EventStatus.UPCOMING, EventStatus.LIVE)
HOLDS_SPACE = (UploadFileStatus.PENDING, UploadFileStatus.FAILED)


def _ttl(name: str) -> timedelta:
    p = get_params()
    minutes = {
        "reservation": p["storage"]["reservation_ttl_minutes"],
        "presign": p["upload"]["presign_ttl_minutes"],
    }[name]
    return timedelta(minutes=minutes)


def _presign(f: UploadFile) -> dict:
    ttl = int(_ttl("presign").total_seconds())
    return storage.presign_put(f.s3_key, f.mime_type, f.size_bytes, f.sha256, ttl)


async def _lock_quota(db: AsyncSession, photographer_id: uuid.UUID) -> StorageQuota:
    return (
        await db.scalars(
            select(StorageQuota)
            .where(StorageQuota.photographer_id == photographer_id)
            .with_for_update()
        )
    ).one()


def _check_open(event: Event) -> None:
    if event.status not in OPEN_EVENT or event.deleted_at is not None:
        raise VedaException("EVENT_CLOSED", f"Uploads are closed for a {event.status} event", 409)


# --- sessions --------------------------------------------------------------------------


async def open_session(
    db: AsyncSession,
    photographer: Photographer,
    event_id: int,
    source: UploadSource,
    device_label: str | None,
) -> UploadSession:
    event = await events.get_owned(db, photographer.id, event_id)
    _check_open(event)
    session = UploadSession(
        event_id=event.id, photographer_id=photographer.id, source=source, device_label=device_label
    )
    db.add(session)
    await db.flush()
    await db.refresh(session)
    return session


async def get_session(
    db: AsyncSession, photographer_id: uuid.UUID, session_id: int
) -> UploadSession:
    session = await db.scalar(
        select(UploadSession).where(
            UploadSession.id == session_id, UploadSession.photographer_id == photographer_id
        )
    )
    if session is None:
        raise VedaException("UPLOAD_SESSION_NOT_FOUND", "Upload session not found", 404)
    return session


async def session_counts(db: AsyncSession, session_id: int) -> dict[str, int]:
    rows = await db.execute(
        select(UploadFile.status, func.count())
        .where(UploadFile.upload_session_id == session_id)
        .group_by(UploadFile.status)
    )
    return {status.value: 0 for status in UploadFileStatus} | dict(rows.all())


# --- reserve -----------------------------------------------------------------------------


async def request_files(
    db: AsyncSession, photographer: Photographer, session_id: int, files: list[dict]
) -> list[dict]:
    """Reserve space for each file and hand back an upload URL, a duplicate or a refusal.

    Files are taken in order until the quota runs out; the answer list matches the input.
    """
    session = await get_session(db, photographer.id, session_id)
    event = await db.get(Event, session.event_id)
    _check_open(event)

    # Lock first: everything below (dedupe included) is then serialized per photographer.
    quota = await _lock_quota(db, photographer.id)
    hashes = {f["sha256"] for f in files}
    taken = set(
        await db.scalars(
            select(Photo.sha256).where(
                Photo.event_id == event.id, Photo.sha256.in_(hashes), Photo.deleted_at.is_(None)
            )
        )
    ) | set(
        await db.scalars(
            select(UploadFile.sha256).where(
                UploadFile.event_id == event.id,
                UploadFile.sha256.in_(hashes),
                UploadFile.status.in_(HOLDS_SPACE),
            )
        )
    )

    free = quota.quota_bytes - quota.used_bytes - quota.reserved_bytes
    expires_at = datetime.now(UTC) + _ttl("reservation")
    answers: list[tuple[dict, UploadDecision, UploadFile | None]] = []
    for f in files:
        if f["sha256"] in taken:
            answers.append((f, UploadDecision.DUPLICATE, None))
        elif f["size_bytes"] > free:
            answers.append((f, UploadDecision.QUOTA_EXCEEDED, None))
        else:
            taken.add(f["sha256"])
            free -= f["size_bytes"]
            upload = UploadFile(
                upload_session_id=session.id,
                event_id=event.id,
                photographer_id=photographer.id,
                original_filename=f["filename"],
                mime_type=f["mime_type"],
                size_bytes=f["size_bytes"],
                sha256=f["sha256"],
                s3_key=storage.original_key(photographer.public_id, event.id, f["mime_type"]),
                reserved_bytes=f["size_bytes"],
                expires_at=expires_at,
            )
            db.add(upload)
            answers.append((f, UploadDecision.UPLOAD, upload))
            quota.reserved_bytes += f["size_bytes"]
    await db.flush()  # ids for the answers

    return [
        {
            "filename": f["filename"],
            "sha256": f["sha256"],
            "decision": decision,
            "file_id": upload.id if upload else None,
            "upload": _presign(upload) if upload else None,
        }
        for f, decision, upload in answers
    ]


# --- per file ------------------------------------------------------------------------------


async def get_file(
    db: AsyncSession, photographer_id: uuid.UUID, file_id: int, *, lock: bool = False
) -> UploadFile:
    query = select(UploadFile).where(
        UploadFile.id == file_id, UploadFile.photographer_id == photographer_id
    )
    if lock:  # populate_existing: overwrite what this session already loaded with the locked row
        query = query.with_for_update().execution_options(populate_existing=True)
    upload = await db.scalar(query)
    if upload is None:
        raise VedaException("UPLOAD_NOT_FOUND", "Upload not found", 404)
    return upload


def _require_open(upload: UploadFile) -> None:
    if upload.status not in HOLDS_SPACE:
        raise VedaException("UPLOAD_CLOSED", f"This upload is {upload.status}", 409)
    if upload.expires_at <= datetime.now(UTC):
        raise VedaException("RESERVATION_EXPIRED", "Request a new upload for this file", 409)


async def complete(
    db: AsyncSession, photographer: Photographer, file_id: int
) -> tuple[UploadFile, Photo | None]:
    """Check S3 has exactly the declared file, then turn reserved space into used space.

    Returns (upload, photo). On a mismatch the upload is marked FAILED and photo is None;
    the caller commits that, so the client can /retry.
    """
    upload = await get_file(db, photographer.id, file_id)
    if upload.status == UploadFileStatus.COMPLETED:  # repeated call: same answer
        return upload, await db.scalar(select(Photo).where(Photo.upload_file_id == upload.id))
    _require_open(upload)

    found = await run_in_threadpool(storage.head, upload.s3_key)  # network: before any lock

    # Re-read under the lock: a parallel call may have completed it meanwhile.
    upload = await get_file(db, photographer.id, file_id, lock=True)
    if upload.status == UploadFileStatus.COMPLETED:
        return upload, await db.scalar(select(Photo).where(Photo.upload_file_id == upload.id))
    _require_open(upload)
    if found is None:
        raise VedaException("NOT_UPLOADED", "The file hasn't reached storage yet", 409)

    size, checksum = found
    # S3 already refused other bytes (length and checksum are signed into the URL);
    # this re-check guards against a misissued URL. Checksum: only when S3 reports one.
    if size != upload.size_bytes or (checksum and checksum != storage.b64_sha256(upload.sha256)):
        upload.status = UploadFileStatus.FAILED
        upload.last_error = f"Stored file doesn't match: {size} bytes vs {upload.size_bytes}"
        return upload, None

    quota = await _lock_quota(db, photographer.id)
    quota.reserved_bytes -= upload.reserved_bytes
    quota.used_bytes += size
    upload.reserved_bytes = 0
    upload.status = UploadFileStatus.COMPLETED
    upload.completed_at = datetime.now(UTC)
    photo = Photo(
        public_id=new_photo_public_id(),
        event_id=upload.event_id,
        photographer_id=upload.photographer_id,
        upload_file_id=upload.id,
        original_filename=upload.original_filename,
        mime_type=upload.mime_type,
        size_bytes=size,
        sha256=upload.sha256,
        s3_key=upload.s3_key,
    )
    db.add(photo)
    await db.flush()
    # Same transaction as the photo: if queueing fails later, the sweep still finds it.
    db.add(ProcessingJob(photo_id=photo.id, job_type=ProcessingJobType.VARIANTS))
    await db.flush()
    await db.refresh(photo)
    return upload, photo  # the route queues processing after it commits


async def retry(db: AsyncSession, photographer_id: uuid.UUID, file_id: int) -> dict:
    """A fresh upload URL for the same reservation (e.g. the connection dropped)."""
    upload = await get_file(db, photographer_id, file_id, lock=True)
    _require_open(upload)
    if upload.attempts >= get_params()["upload"]["max_attempts"]:
        raise VedaException("TOO_MANY_ATTEMPTS", "Abort this upload and start again", 409)
    upload.attempts += 1
    upload.status = UploadFileStatus.PENDING
    upload.last_error = None
    upload.expires_at = datetime.now(UTC) + _ttl("reservation")
    return _presign(upload)


async def abort(db: AsyncSession, photographer_id: uuid.UUID, file_id: int) -> str:
    """Give the space back. Returns the S3 key for the caller to delete after commit."""
    upload = await get_file(db, photographer_id, file_id, lock=True)
    if upload.status not in HOLDS_SPACE:
        raise VedaException("UPLOAD_CLOSED", f"This upload is {upload.status}", 409)
    quota = await _lock_quota(db, photographer_id)
    _release(quota, upload, UploadFileStatus.CANCELLED)
    return upload.s3_key


def _release(quota: StorageQuota, upload: UploadFile, status: UploadFileStatus) -> None:
    quota.reserved_bytes -= upload.reserved_bytes
    upload.reserved_bytes = 0
    upload.status = status


def release_expired(s: Session, now: datetime) -> list[str]:
    """Beat job (sync). Frees space held by uploads that never completed.

    Returns their S3 keys: an object may have arrived without /complete being called.
    """
    expired = s.scalars(
        select(UploadFile)
        .where(UploadFile.status.in_(HOLDS_SPACE), UploadFile.expires_at <= now)
        .order_by(UploadFile.id)
        .limit(500)
        .with_for_update(skip_locked=True)  # a client completing right now keeps its row
    ).all()
    for upload in expired:
        quota = s.scalars(
            select(StorageQuota)
            .where(StorageQuota.photographer_id == upload.photographer_id)
            .with_for_update()
        ).one()
        _release(quota, upload, UploadFileStatus.EXPIRED)
    return [u.s3_key for u in expired]


async def storage_summary(db: AsyncSession, photographer_id: uuid.UUID) -> dict:
    quota = await db.get(StorageQuota, photographer_id)
    q, used, reserved = (
        (quota.quota_bytes, quota.used_bytes, quota.reserved_bytes) if quota else (0, 0, 0)
    )
    return {
        "quota_bytes": q,
        "used_bytes": used,
        "reserved_bytes": reserved,
        "available_bytes": max(q - used - reserved, 0),
    }
