"""Sign-in, sign-out and token refresh happen in React via Supabase Auth.
The API only turns a signed-in Supabase user into a photographer record."""

from fastapi import APIRouter
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy.exc import IntegrityError

from veda.api.deps import CurrentAdmin, CurrentPhotographer, CurrentUser, DbSession
from veda.db.models import Photographer
from veda.exception import VedaException
from veda.utils.common import new_photographer_public_id

router = APIRouter(tags=["auth"])


class RegisterIn(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True)

    full_name: str = Field(min_length=2, max_length=120)
    studio_name: str | None = Field(default=None, max_length=160)
    phone: str = Field(pattern=r"^\+[1-9]\d{7,14}$", examples=["+919876543210"])  # E.164
    city: str | None = Field(default=None, max_length=80)


class PhotographerOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    public_id: str
    full_name: str
    studio_name: str | None
    email: str
    phone: str
    city: str | None
    status: str


# Which unique constraint a duplicate hit → what to tell the user.
_DUPLICATES = {
    "pk_photographers": ("ALREADY_REGISTERED", "You are already registered"),
    "uq_photographers_email": ("EMAIL_TAKEN", "This email is already registered"),
    "uq_photographers_phone": ("PHONE_TAKEN", "This phone number is already registered"),
}


@router.post("/auth/register", status_code=201, response_model=PhotographerOut)
async def register(body: RegisterIn, user: CurrentUser, db: DbSession):
    if not user.email:
        raise VedaException("EMAIL_REQUIRED", "Sign in with email to register", status=400)
    for _ in range(5):  # retry only on the (rare) public_id collision
        photographer = Photographer(
            id=user.id,
            public_id=new_photographer_public_id(),
            email=user.email.lower(),
            **body.model_dump(),
        )
        db.add(photographer)
        try:
            await db.commit()
            return photographer
        except IntegrityError as e:
            await db.rollback()
            error = str(e.orig)
            if "uq_photographers_public_id" in error:
                continue
            for constraint, (code, message) in _DUPLICATES.items():
                if constraint in error:
                    raise VedaException(code, message, status=409) from e
            raise
    raise VedaException("ID_GENERATION_FAILED", "Please try again", status=500)


@router.get("/me", response_model=PhotographerOut)
async def me(photographer: CurrentPhotographer):
    return photographer


@router.get("/admin/me")
async def admin_me(admin: CurrentAdmin):
    return {"id": str(admin.id), "email": admin.email}
