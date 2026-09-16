import logging
import re
from time import perf_counter
from uuid import uuid4

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

from app import db
from app.config import settings
from app.logging_config import configure_logging

configure_logging()
logger = logging.getLogger(__name__)
request_id_pattern = re.compile(r"^[A-Za-z0-9-]{1,64}$")

app = FastAPI(title=settings.app_name, version=settings.app_version)


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
