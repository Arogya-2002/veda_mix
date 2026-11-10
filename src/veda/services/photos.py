"""Photo processing state, for the Celery worker (sync sessions).

A photo is born PROCESSING with a QUEUED job (see uploads.complete). The worker moves it
to READY with its variants, or FAILED; the job row records attempts and the last error.
"""

from datetime import UTC, datetime, timedelta

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from veda.constants.enums import (
    PhotoStatus,
    PhotoVariantName,
    ProcessingJobStatus,
    ProcessingJobType,
)
from veda.db.models import Photo, PhotoVariant, ProcessingJob
from veda.entity.artifact_entity import ProcessedImage

OPEN_JOB = (ProcessingJobStatus.QUEUED, ProcessingJobStatus.RUNNING)


def live_fields(photo: Photo, variant_keys: dict[str, str]) -> dict:
    """A READY photo as the gallery needs it: JSON-safe, S3 keys rather than URLs.
    The same shape is published to live streams and read back for gallery pages."""
    return {
        "public_id": photo.public_id,
        "width": photo.width,
        "height": photo.height,
        "taken_at": photo.taken_at.isoformat() if photo.taken_at else None,
        "uploaded_at": photo.uploaded_at.isoformat(),
        "thumb_key": variant_keys[PhotoVariantName.THUMB],
        "web_key": variant_keys[PhotoVariantName.WEB],
    }


def _job(s: Session, photo_id: int) -> ProcessingJob:
    job = s.scalars(
        select(ProcessingJob)
        .where(
            ProcessingJob.photo_id == photo_id,
            ProcessingJob.job_type == ProcessingJobType.VARIANTS,
        )
        .with_for_update()
    ).one_or_none()
    if job is None:  # defensive: every photo gets one at upload
        job = ProcessingJob(photo_id=photo_id, job_type=ProcessingJobType.VARIANTS, attempts=0)
        s.add(job)
    return job


def _locked_photo(s: Session, photo_id: int) -> Photo | None:
    return s.scalars(select(Photo).where(Photo.id == photo_id).with_for_update()).one_or_none()


def start(s: Session, photo_id: int) -> Photo | None:
    """Mark the job RUNNING. None when there's nothing to do (already READY or FAILED, or
    deleted): duplicate queue messages are expected and harmless."""
    photo = s.get(Photo, photo_id)
    if photo is None or photo.status != PhotoStatus.PROCESSING or photo.deleted_at is not None:
        return None
    job = _job(s, photo_id)
    job.status = ProcessingJobStatus.RUNNING
    job.attempts += 1
    job.started_at = datetime.now(UTC)
    return photo


def finish(s: Session, photo_id: int, result: ProcessedImage, keys: dict[str, str]) -> Photo | None:
    """Store the variants and turn the photo READY. None if another run already did."""
    photo = _locked_photo(s, photo_id)
    if photo is None or photo.status != PhotoStatus.PROCESSING:
        return None
    for v in result.variants:
        s.add(
            PhotoVariant(
                photo_id=photo_id,
                variant=v.name,
                s3_key=keys[v.name],
                format=v.format,
                width=v.width,
                height=v.height,
                size_bytes=len(v.data),
            )
        )
    photo.width, photo.height = result.width, result.height
    photo.taken_at = result.taken_at
    photo.camera_make, photo.camera_model = result.camera_make, result.camera_model
    photo.status = PhotoStatus.READY
    photo.ready_at = datetime.now(UTC)

    job = _job(s, photo_id)
    job.status = ProcessingJobStatus.SUCCEEDED
    job.last_error = None
    job.finished_at = photo.ready_at
    return photo


def fail(s: Session, photo_id: int, error: str) -> None:
    photo = _locked_photo(s, photo_id)
    if photo is not None and photo.status == PhotoStatus.PROCESSING:
        photo.status = PhotoStatus.FAILED
    job = _job(s, photo_id)
    job.status = ProcessingJobStatus.FAILED
    job.last_error = error[:1000]
    job.finished_at = datetime.now(UTC)


def requeue_stuck(s: Session, now: datetime, after: timedelta, max_attempts: int) -> list[int]:
    """Beat job. Finds work that went missing: the queue message was lost (Redis down when
    the upload completed) or the worker died for good. Returns photo ids to queue again."""
    stuck = s.scalars(
        select(ProcessingJob)
        .where(
            ProcessingJob.status.in_(OPEN_JOB),
            func.coalesce(ProcessingJob.started_at, ProcessingJob.queued_at) <= now - after,
        )
        .order_by(ProcessingJob.id)
        .limit(500)
        .with_for_update(skip_locked=True)
    ).all()
    requeue = []
    for job in stuck:
        if job.attempts >= max_attempts:
            fail(s, job.photo_id, f"Gave up after {job.attempts} attempts")
        else:
            job.status = ProcessingJobStatus.QUEUED
            job.queued_at, job.started_at = now, None
            requeue.append(job.photo_id)
    return requeue
