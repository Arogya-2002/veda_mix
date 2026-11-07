import uuid

import structlog
from fastapi import FastAPI, Request

from veda.api import auth, health, payments
from veda.exception import register_exception_handlers
from veda.logging import setup_logging

setup_logging()

app = FastAPI(title="Veda Live Photography API", version="0.1.0")
register_exception_handlers(app)
app.include_router(health.router)
app.include_router(auth.router)
app.include_router(payments.router)


@app.middleware("http")
async def request_id(request: Request, call_next):
    rid = request.headers.get("x-request-id") or uuid.uuid4().hex
    structlog.contextvars.bind_contextvars(request_id=rid)
    try:
        response = await call_next(request)
    finally:
        structlog.contextvars.clear_contextvars()
    response.headers["x-request-id"] = rid
    return response
