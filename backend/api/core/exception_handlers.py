import logging
from fastapi import FastAPI, Request, HTTPException
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse

logger = logging.getLogger(__name__)


def register_exception_handlers(app: FastAPI):
    from scripts.config import settings

    @app.exception_handler(HTTPException)
    async def http_exception_handler(request: Request, exc: HTTPException):
        return JSONResponse(
            status_code=exc.status_code,
            content={"detail": exc.detail},
            headers=getattr(exc, "headers", None) or {},
        )

    @app.exception_handler(RequestValidationError)
    async def validation_exception_handler(request: Request, exc: RequestValidationError):
        """Return one readable sentence instead of pydantic's error array.

        Clients render `detail` directly; handing them a list of dicts means
        the user sees a generic "something went wrong" instead of the actual
        problem ("Password must be at least 8 characters").
        """
        messages = []
        for err in exc.errors():
            msg = err.get("msg", "Invalid value")
            # Pydantic v2 prefixes custom ValueErrors with "Value error, ".
            msg = msg.replace("Value error, ", "")
            field = next(
                (str(p) for p in err.get("loc", ()) if p not in ("body", "query", "path")),
                None,
            )
            messages.append(f"{field.replace('_', ' ').capitalize()}: {msg}" if field else msg)

        return JSONResponse(
            status_code=422,
            content={"detail": " ".join(messages) or "Invalid request."},
        )

    @app.exception_handler(Exception)
    async def generic_exception_handler(request: Request, exc: Exception):
        logger.error(f"Unhandled exception on {request.method} {request.url.path}: {exc}", exc_info=True)
        detail = "An internal error occurred"
        if settings.DEBUG:
            detail = str(exc)
        return JSONResponse(
            status_code=500,
            content={"detail": detail},
        )

    @app.exception_handler(404)
    async def not_found_handler(request: Request, exc: Exception):
        return JSONResponse(
            status_code=404,
            content={"detail": "Not found"},
        )
