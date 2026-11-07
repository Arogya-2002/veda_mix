from datetime import datetime

from fastapi import APIRouter
from fastapi.concurrency import run_in_threadpool
from pydantic import BaseModel, ConfigDict, Field, field_validator

from veda.api.deps import ActivePhotographer, CurrentPhotographer, DbSession
from veda.config.configuration import get_schema
from veda.constants.enums import UploadDecision, UploadSource
from veda.exception import VedaException
from veda.integrations import storage
from veda.services import uploads

router = APIRouter(tags=["uploads"])


class SessionIn(BaseModel):
    source: UploadSource = UploadSource.BROWSER
    device_label: str | None = Field(default=None, max_length=120)


class SessionOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: int
    event_id: int
    source: str
    device_label: str | None
    created_at: datetime


class SessionStatusOut(SessionOut):
    files: dict[str, int]  # count per upload status


class FileIn(BaseModel):
    filename: str = Field(min_length=1, max_length=255)
    size_bytes: int = Field(gt=0)
    mime_type: str
    sha256: str = Field(pattern=r"^[0-9a-f]{64}$", description="Lowercase hex of the file")

    @field_validator("mime_type")
    @classmethod
    def _allowed_type(cls, v: str) -> str:
        allowed = get_schema()["upload"]["allowed_mime_types"]
        if v not in allowed:
            raise ValueError(f"must be one of {allowed}")
        return v

    @field_validator("size_bytes")
    @classmethod
    def _max_size(cls, v: int) -> int:
        limit = get_schema()["upload"]["max_file_bytes"]
        if v > limit:
            raise ValueError(f"must be at most {limit} bytes")
        return v


class FilesIn(BaseModel):
    files: list[FileIn] = Field(min_length=1, max_length=50)


class UploadInstructions(BaseModel):
    method: str
    url: str
    headers: dict[str, str]  # send exactly these, or S3 rejects the upload


class FileDecisionOut(BaseModel):
    filename: str
    sha256: str
    decision: UploadDecision
    file_id: int | None
    upload: UploadInstructions | None


class PhotoOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    public_id: str
    event_id: int
    original_filename: str
    size_bytes: int
    status: str
    uploaded_at: datetime


class StorageOut(BaseModel):
    quota_bytes: int
    used_bytes: int
    reserved_bytes: int
    available_bytes: int


@router.post("/events/{event_id}/upload-sessions", status_code=201, response_model=SessionOut)
async def open_session(
    event_id: int, body: SessionIn, photographer: ActivePhotographer, db: DbSession
):
    session = await uploads.open_session(db, photographer, event_id, body.source, body.device_label)
    await db.commit()
    return session


@router.get("/upload-sessions/{session_id}", response_model=SessionStatusOut)
async def session_status(session_id: int, photographer: CurrentPhotographer, db: DbSession):
    session = await uploads.get_session(db, photographer.id, session_id)
    files = await uploads.session_counts(db, session.id)
    return SessionStatusOut(**SessionOut.model_validate(session).model_dump(), files=files)


@router.post("/upload-sessions/{session_id}/files", response_model=list[FileDecisionOut])
async def request_files(
    session_id: int, body: FilesIn, photographer: ActivePhotographer, db: DbSession
):
    """Reserve space and get one upload URL per file (up to 50 per call)."""
    decisions = await uploads.request_files(
        db, photographer, session_id, [f.model_dump() for f in body.files]
    )
    await db.commit()
    return decisions


@router.post("/uploads/{file_id}/complete", response_model=PhotoOut)
async def complete(file_id: int, photographer: CurrentPhotographer, db: DbSession):
    # CurrentPhotographer, not Active: space was reserved while active, so finishing is fair.
    upload, photo = await uploads.complete(db, photographer, file_id)
    await db.commit()
    if photo is None:
        raise VedaException("UPLOAD_MISMATCH", upload.last_error or "Upload failed", 422)
    return photo


@router.post("/uploads/{file_id}/retry", response_model=UploadInstructions)
async def retry(file_id: int, photographer: ActivePhotographer, db: DbSession):
    instructions = await uploads.retry(db, photographer.id, file_id)
    await db.commit()
    return instructions


@router.post("/uploads/{file_id}/abort", status_code=204)
async def abort(file_id: int, photographer: CurrentPhotographer, db: DbSession):
    key = await uploads.abort(db, photographer.id, file_id)
    await db.commit()
    await run_in_threadpool(storage.delete, key)  # after commit; a leftover is harmless


@router.get("/me/storage", response_model=StorageOut)
async def my_storage(photographer: CurrentPhotographer, db: DbSession):
    return await uploads.storage_summary(db, photographer.id)
