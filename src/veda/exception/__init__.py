from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse

from veda.logging import get_logger

log = get_logger(__name__)


class VedaException(Exception):
    """Raise anywhere; the API turns it into the shared error shape."""

    def __init__(self, code: str, message: str, status: int = 400, details: dict | None = None):
        super().__init__(message)
        self.code, self.message, self.status, self.details = code, message, status, details or {}


def _error(status: int, code: str, message: str, details: dict | None = None) -> JSONResponse:
    body = {"error": {"code": code, "message": message, "details": details or {}}}
    return JSONResponse(status_code=status, content=body)


def register_exception_handlers(app: FastAPI) -> None:
    @app.exception_handler(VedaException)
    async def _veda(_: Request, exc: VedaException):
        return _error(exc.status, exc.code, exc.message, exc.details)

    @app.exception_handler(RequestValidationError)
    async def _validation(_: Request, exc: RequestValidationError):
        # Drop "ctx": it can hold the raised ValueError itself, which isn't JSON.
        errors = [{k: v for k, v in e.items() if k != "ctx"} for e in exc.errors()]
        return _error(422, "VALIDATION_ERROR", "Invalid request", {"errors": errors})

    @app.exception_handler(Exception)
    async def _unhandled(_: Request, exc: Exception):
        log.exception("unhandled_error")
        return _error(500, "INTERNAL_ERROR", "Something went wrong")
