from fastapi import APIRouter
from fastapi.responses import JSONResponse
from redis.asyncio import Redis
from sqlalchemy import text

from veda.config.configuration import get_settings
from veda.db import engine

router = APIRouter(prefix="/health", tags=["health"])


@router.get("/live")
async def live():
    return {"status": "ok"}


@router.get("/ready")
async def ready():
    checks = {}
    try:
        async with engine.connect() as conn:
            await conn.execute(text("SELECT 1"))
        checks["db"] = "ok"
    except Exception as e:  # noqa: BLE001 - report any failure as not ready
        checks["db"] = f"error: {type(e).__name__}"
    try:
        async with Redis.from_url(get_settings().redis_url) as r:
            await r.ping()
        checks["redis"] = "ok"
    except Exception as e:  # noqa: BLE001 - report any failure as not ready
        checks["redis"] = f"error: {type(e).__name__}"
    ok = all(v == "ok" for v in checks.values())
    return JSONResponse(
        status_code=200 if ok else 503,
        content={"status": "ok" if ok else "degraded", "checks": checks},
    )
