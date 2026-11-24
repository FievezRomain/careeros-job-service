import json
import logging
import re
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager
from uuid import uuid4

from fastapi import FastAPI, Request, Response, status
from pydantic import BaseModel, ConfigDict

from careeros_job.application.startup import StartService
from careeros_job.domain.health import ServiceState

_CORRELATION_PATTERN = re.compile(r"^[A-Za-z0-9._-]{1,128}$")


class HealthResponse(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    service: str = "careeros-job-service"
    state: ServiceState


class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        return json.dumps(
            {
                "level": record.levelname.lower(),
                "event": record.getMessage(),
                "correlation_id": getattr(record, "correlation_id", None),
                "method": getattr(record, "method", None),
                "path": getattr(record, "path", None),
                "status": getattr(record, "status", None),
            },
            separators=(",", ":"),
        )


def create_http_app(start_service: StartService, logger: logging.Logger | None = None) -> FastAPI:
    request_logger = logger or logging.getLogger("careeros_job.requests")

    @asynccontextmanager
    async def lifespan(_: FastAPI) -> AsyncIterator[None]:
        start_service.execute()
        yield

    app = FastAPI(title="CareerOS Job Service", version="0.1.0", lifespan=lifespan)

    @app.middleware("http")
    async def correlation_middleware(
        request: Request, call_next: Callable[[Request], Awaitable[Response]]
    ) -> Response:
        requested = request.headers.get("X-Correlation-ID", "")
        correlation_id = requested if _CORRELATION_PATTERN.fullmatch(requested) else uuid4().hex
        response = await call_next(request)
        response.headers["X-Correlation-ID"] = correlation_id
        request_logger.info(
            "request_completed",
            extra={
                "correlation_id": correlation_id,
                "method": request.method,
                "path": request.url.path,
                "status": response.status_code,
            },
        )
        return response

    @app.get("/health/live", response_model=HealthResponse)
    def liveness() -> HealthResponse:
        return HealthResponse(state=ServiceState.READY)

    @app.get("/health/ready", response_model=HealthResponse)
    def readiness(response: Response) -> HealthResponse:
        current = start_service.status.state
        if current is not ServiceState.READY:
            response.status_code = status.HTTP_503_SERVICE_UNAVAILABLE
        return HealthResponse(state=current)

    return app
