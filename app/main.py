import logging
import re
from time import perf_counter
from uuid import uuid4

from fastapi import FastAPI, HTTPException, Request
from fastapi.encoders import jsonable_encoder
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse

from app import db
from app.api import accounts, auth, collections, portal, review, classification
from app.config import settings
from app.errors import APIError
from app.logging_config import configure_logging

configure_logging()
logger = logging.getLogger(__name__)
request_id_pattern = re.compile(r"^[A-Za-z0-9-]{1,64}$")

app = FastAPI(title=settings.app_name, version=settings.app_version)
app.include_router(auth.router)
app.include_router(accounts.router)
app.include_router(collections.router)
app.include_router(collections.requirements_router)
app.include_router(portal.router)
app.include_router(review.router)
app.include_router(classification.router)


def error_response(
    request: Request, status_code: int, code: str, message: str, details=None
):
    return JSONResponse(
        status_code=status_code,
        content={
            "code": code,
            "message": message,
            "details": jsonable_encoder(details),
            "request_id": getattr(request.state, "request_id", None),
        },
    )


@app.exception_handler(APIError)
def api_error_handler(request: Request, exc: APIError):
    return error_response(request, exc.status_code, exc.code, exc.message, exc.details)


@app.exception_handler(RequestValidationError)
def validation_error_handler(request: Request, exc: RequestValidationError):
    details = [
        {key: value for key, value in error.items() if key != "input"}
        for error in exc.errors()
    ]
    return error_response(
        request, 422, "VALIDATION_ERROR", "Request validation failed", details
    )


@app.exception_handler(HTTPException)
def http_error_handler(request: Request, exc: HTTPException):
    return error_response(request, exc.status_code, "HTTP_ERROR", str(exc.detail))


@app.exception_handler(Exception)
def unhandled_error_handler(request: Request, exc: Exception):
    logger.exception("Unhandled application error", exc_info=exc)
    return error_response(request, 500, "INTERNAL_ERROR", "An unexpected error occurred")


@app.middleware("http")
async def request_context(request: Request, call_next):
    incoming_request_id = request.headers.get("X-Request-ID", "")
    request_id = (
        incoming_request_id
        if request_id_pattern.fullmatch(incoming_request_id)
        else str(uuid4())
    )
    request.state.request_id = request_id
    started_at = perf_counter()

    try:
        response = await call_next(request)
    except Exception:
        logger.exception(
            "Unhandled request error",
            extra={
                "request_id": request_id,
                "method": request.method,
                "path": request.url.path,
            },
        )
        raise

    response.headers["X-Request-ID"] = request_id
    logger.info(
        "Request completed",
        extra={
            "request_id": request_id,
            "method": request.method,
            "path": request.url.path,
            "status_code": response.status_code,
            "duration_ms": round((perf_counter() - started_at) * 1000, 2),
        },
    )
    return response


@app.get("/api/v1/health/live")
def live() -> dict[str, str]:
    return {"status": "ok", "version": settings.app_version}


@app.get("/api/v1/health/ready")
def ready(request: Request):
    dependencies = db.dependency_status()
    if all(value == "ok" for value in dependencies.values()):
        return {"status": "ok", "dependencies": dependencies}

    return JSONResponse(
        status_code=503,
        content={
            "code": "SERVICE_NOT_READY",
            "message": "Required service is unavailable",
            "details": dependencies,
            "request_id": request.state.request_id,
        },
    )
