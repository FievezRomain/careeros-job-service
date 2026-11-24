import json
import logging
import re
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager
from typing import Annotated, Protocol, cast
from uuid import uuid4

import jwt
from fastapi import Depends, FastAPI, Header, HTTPException, Request, Response, Security, status
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from jwt import PyJWK, PyJWKClient, PyJWTError
from pydantic import BaseModel, ConfigDict, Field, ValidationError

from careeros_job.application.manual_capture import (
    AccountContext,
    CaptureManualOpportunity,
    IdempotencyKeyConflict,
    PermissionDenied,
    TokenValidationError,
    TokenVerifier,
)
from careeros_job.application.startup import StartService
from careeros_job.domain.health import ServiceState
from careeros_job.domain.opportunity import ManualOpportunityDraft, Opportunity

_CORRELATION_PATTERN = re.compile(r"^[A-Za-z0-9._-]{1,128}$")
_IDEMPOTENCY_PATTERN = re.compile(r"^[A-Za-z0-9._:-]{16,128}$")
_BEARER = HTTPBearer(auto_error=False)


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


class Problem(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, populate_by_name=True)

    type: str
    title: str
    status: int
    detail: str
    code: str
    correlation_id: str = Field(alias="correlationId")


class SigningKeyProvider(Protocol):
    def resolve(self, token: str) -> PyJWK: ...


class SigningKeyResolver:
    def __init__(self, jwks_url: str) -> None:
        self._client = PyJWKClient(jwks_url, cache_keys=True)

    def resolve(self, token: str) -> PyJWK:
        return self._client.get_signing_key_from_jwt(token)


class JwtTokenVerifier:
    def __init__(self, resolver: SigningKeyProvider, issuer: str, audience: str) -> None:
        self._resolver = resolver
        self._issuer = issuer
        self._audience = audience

    def verify(self, token: str) -> AccountContext:
        try:
            claims = jwt.decode(
                token,
                key=self._resolver.resolve(token),
                algorithms=["RS256"],
                issuer=self._issuer,
                audience=self._audience,
                options={"require": ["exp", "iat", "sub", "tenant_id"]},
            )
            scope_value = claims.get("scope", "")
            scopes = tuple(sorted(set(scope_value.split()))) if isinstance(scope_value, str) else ()
            return AccountContext.model_validate(
                {
                    "account_id": claims.get("sub"),
                    "tenant_id": claims.get("tenant_id"),
                    "scopes": scopes,
                }
            )
        except (PyJWTError, ValidationError, TypeError):
            raise TokenValidationError from None


def _http_error(status_code: int, code: str, title: str, detail: str) -> HTTPException:
    return HTTPException(
        status_code=status_code,
        detail={"code": code, "title": title, "detail": detail},
    )


def create_http_app(
    start_service: StartService,
    logger: logging.Logger | None = None,
    *,
    capture_manual: CaptureManualOpportunity | None = None,
    token_verifier: TokenVerifier | None = None,
    shutdown: Callable[[], None] | None = None,
) -> FastAPI:
    request_logger = logger or logging.getLogger("careeros_job.requests")

    @asynccontextmanager
    async def lifespan(_: FastAPI) -> AsyncIterator[None]:
        try:
            start_service.execute()
            yield
        finally:
            if shutdown is not None:
                shutdown()

    app = FastAPI(title="CareerOS Job Service", version="0.1.0", lifespan=lifespan)

    @app.middleware("http")
    async def correlation_middleware(
        request: Request, call_next: Callable[[Request], Awaitable[Response]]
    ) -> Response:
        requested = request.headers.get("X-Correlation-ID", "")
        correlation_id = requested if _CORRELATION_PATTERN.fullmatch(requested) else uuid4().hex
        request.state.correlation_id = correlation_id
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

    if capture_manual is not None and token_verifier is not None:
        app.state.token_verifier = token_verifier

        def require_account_context(
            credentials: Annotated[HTTPAuthorizationCredentials | None, Security(_BEARER)],
        ) -> AccountContext:
            if credentials is None or credentials.scheme.lower() != "bearer":
                raise _http_error(
                    401, "UNAUTHENTICATED", "Unauthenticated", "A valid bearer token is required."
                )
            verifier = cast(TokenVerifier, app.state.token_verifier)
            try:
                return verifier.verify(credentials.credentials)
            except TokenValidationError:
                raise _http_error(
                    401,
                    "UNAUTHENTICATED",
                    "Unauthenticated",
                    "A valid bearer token is required.",
                ) from None

        @app.exception_handler(HTTPException)
        async def http_exception_handler(request: Request, error: HTTPException) -> JSONResponse:
            details: dict[str, object] = error.detail if isinstance(error.detail, dict) else {}
            problem_code = str(details.get("code", "REQUEST_FAILED"))
            problem = Problem(
                type=f"https://api.careeros.invalid/problems/{problem_code.lower()}",
                title=str(details.get("title", "Request failed")),
                status=error.status_code,
                detail=str(details.get("detail", "The request could not be completed.")),
                code=problem_code,
                correlation_id=request.state.correlation_id,
            )
            headers = {"WWW-Authenticate": "Bearer"} if error.status_code == 401 else None
            return JSONResponse(
                problem.model_dump(by_alias=True),
                status_code=error.status_code,
                media_type="application/problem+json",
                headers=headers,
            )

        @app.exception_handler(RequestValidationError)
        async def validation_exception_handler(
            request: Request, error: RequestValidationError
        ) -> JSONResponse:
            header_error = any(issue.get("loc", ())[:1] == ("header",) for issue in error.errors())
            problem = Problem(
                type="https://api.careeros.invalid/problems/validation-failed",
                title="Validation failed",
                status=400 if header_error else 422,
                detail=(
                    "Idempotency-Key must contain 16 to 128 allowed characters."
                    if header_error
                    else "The request does not satisfy the manual opportunity contract."
                ),
                code="IDEMPOTENCY_KEY_INVALID" if header_error else "VALIDATION_FAILED",
                correlation_id=request.state.correlation_id,
            )
            return JSONResponse(
                problem.model_dump(by_alias=True),
                status_code=problem.status,
                media_type="application/problem+json",
            )

        Account = Annotated[AccountContext, Depends(require_account_context)]

        @app.post(
            "/v1/jobs/manual",
            response_model=Opportunity,
            response_model_by_alias=True,
            status_code=201,
            responses={
                200: {"model": Opportunity, "description": "Idempotent replay"},
                400: {"model": Problem, "description": "Invalid idempotency key"},
                401: {"model": Problem, "description": "Unauthenticated"},
                403: {"model": Problem, "description": "Missing jobs:write scope"},
                409: {"model": Problem, "description": "Idempotency key conflict"},
                422: {"model": Problem, "description": "Invalid request body"},
            },
            tags=["Jobs"],
        )
        def capture_manual_job(
            draft: ManualOpportunityDraft,
            response: Response,
            account: Account,
            idempotency_key: Annotated[
                str,
                Header(
                    alias="Idempotency-Key",
                    min_length=16,
                    max_length=128,
                    pattern=_IDEMPOTENCY_PATTERN.pattern,
                ),
            ],
        ) -> Opportunity:
            try:
                result = capture_manual.execute(account, draft, idempotency_key)
            except PermissionDenied:
                raise _http_error(
                    403, "FORBIDDEN", "Forbidden", "The jobs:write scope is required."
                ) from None
            except IdempotencyKeyConflict:
                raise _http_error(
                    409,
                    "IDEMPOTENCY_KEY_REUSED",
                    "Idempotency key reused",
                    "The idempotency key was already used for another request.",
                ) from None
            response.status_code = 201 if result.created else 200
            response.headers["Location"] = f"/v1/jobs/{result.opportunity.opportunity_id}"
            return result.opportunity

    return app
