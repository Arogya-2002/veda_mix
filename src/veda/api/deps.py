"""Auth dependencies. Supabase issues the tokens; we only verify them and look up our rows."""

import uuid
from dataclasses import dataclass
from functools import lru_cache
from typing import Annotated

import jwt
from fastapi import Depends
from fastapi.concurrency import run_in_threadpool
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from sqlalchemy.ext.asyncio import AsyncSession

from veda.config.configuration import get_settings
from veda.constants.enums import PhotographerStatus
from veda.db import get_db
from veda.db.models import Admin, Photographer
from veda.exception import VedaException

DbSession = Annotated[AsyncSession, Depends(get_db)]

_bearer = HTTPBearer(auto_error=False)  # auto_error=False: we raise our own error shape


@dataclass(frozen=True)
class AuthUser:
    id: uuid.UUID
    email: str | None
    aal: str  # "aal1" = signed in, "aal2" = also passed MFA (TOTP)


@lru_cache
def _jwks() -> jwt.PyJWKClient:
    # Supabase's public signing keys. PyJWKClient caches them and refetches on an unknown kid.
    url = get_settings().supabase_url.rstrip("/")
    return jwt.PyJWKClient(f"{url}/auth/v1/.well-known/jwks.json", cache_keys=True)


def _signing_key(token: str):
    """Public key matching the token's `kid` header. Tests replace this function."""
    return _jwks().get_signing_key_from_jwt(token).key


async def current_user(
    creds: Annotated[HTTPAuthorizationCredentials | None, Depends(_bearer)],
) -> AuthUser:
    if creds is None:
        raise VedaException("UNAUTHENTICATED", "Missing bearer token", status=401)
    token = creds.credentials
    try:
        # Key lookup may hit the network (urllib, blocking), so keep it off the event loop.
        key = await run_in_threadpool(_signing_key, token)
        claims = jwt.decode(
            token,
            key,
            algorithms=["ES256", "RS256"],  # asymmetric only; never accept HS256 or "none"
            audience="authenticated",
            issuer=get_settings().supabase_url.rstrip("/") + "/auth/v1",
            options={"require": ["exp", "sub", "aud", "iss"]},
        )
        user_id = uuid.UUID(claims["sub"])
    except jwt.exceptions.PyJWKClientConnectionError as e:
        raise VedaException("AUTH_UNAVAILABLE", "Could not reach the auth server", 503) from e
    except (jwt.PyJWTError, ValueError) as e:
        raise VedaException("UNAUTHENTICATED", "Invalid or expired token", status=401) from e
    return AuthUser(id=user_id, email=claims.get("email"), aal=claims.get("aal", "aal1"))


CurrentUser = Annotated[AuthUser, Depends(current_user)]


async def current_photographer(user: CurrentUser, db: DbSession) -> Photographer:
    photographer = await db.get(Photographer, user.id)
    if photographer is None:
        raise VedaException("NOT_REGISTERED", "Complete registration first", status=403)
    if photographer.status in (PhotographerStatus.SUSPENDED, PhotographerStatus.DELETED):
        raise VedaException("ACCOUNT_DISABLED", "This account is disabled", status=403)
    return photographer


CurrentPhotographer = Annotated[Photographer, Depends(current_photographer)]


async def current_admin(user: CurrentUser, db: DbSession) -> AuthUser:
    if await db.get(Admin, user.id) is None:
        raise VedaException("FORBIDDEN", "Admins only", status=403)
    if user.aal != "aal2":
        raise VedaException("MFA_REQUIRED", "Verify your authenticator code first", status=403)
    return user


CurrentAdmin = Annotated[AuthUser, Depends(current_admin)]
