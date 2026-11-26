from datetime import UTC, datetime
from uuid import UUID, uuid4

import httpx2
from fastapi import FastAPI
from pydantic import AnyHttpUrl, BaseModel, ConfigDict, Field, SecretStr, model_validator
from pydantic_settings import SettingsConfigDict
from sqlalchemy import create_engine

from careeros_job.adapters.http import JwtTokenVerifier, SigningKeyResolver, create_http_app
from careeros_job.adapters.postgresql.discovery import SqlAlchemyDiscoveryRepository
from careeros_job.adapters.postgresql.migrations.runner import (
    DatabaseSettings,
    PostgresMigrationRunner,
)
from careeros_job.adapters.postgresql.repository import SqlAlchemyOpportunityRepository
from careeros_job.adapters.sources.greenhouse import (
    GreenhouseGateway,
    UrlLibTransport,
)
from careeros_job.adapters.sources.open_web import DiscoverySources, OpenWebGateway
from careeros_job.application.discovery import (
    RunDailyDiscovery,
    SourceDefinition,
    SynchronizeSearchSnapshot,
)
from careeros_job.application.manual_capture import (
    CaptureManualOpportunity,
    QueryOpportunityCatalog,
)
from careeros_job.application.startup import StartService


class GreenhouseBoardSettings(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    source_id: str = Field(min_length=1, max_length=200)
    board_token: str = Field(min_length=1, max_length=200)
    employer: str = Field(min_length=1, max_length=200)


class ServiceSettings(DatabaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore", frozen=True)

    oidc_issuer: AnyHttpUrl
    oidc_audience: str = Field(min_length=1)
    oidc_jwks_url: AnyHttpUrl
    greenhouse_boards: tuple[GreenhouseBoardSettings, ...] = ()
    ai_gateway_url: AnyHttpUrl | None = None
    ai_gateway_token: SecretStr | None = None

    @model_validator(mode="after")
    def validate_web_configuration(self) -> ServiceSettings:
        url = self.ai_gateway_url
        if (url is None) != (self.ai_gateway_token is None):
            raise ValueError("AI_GATEWAY_URL and AI_GATEWAY_TOKEN must be configured together")
        if url is not None and (
            url.username
            or url.password
            or url.query
            or url.fragment
            or (url.scheme != "https" and url.host not in {"localhost", "127.0.0.1", "[::1]"})
        ):
            raise ValueError("AI_GATEWAY_URL must be an HTTPS service URL or local loopback")
        return self


class SystemClock:
    def now(self) -> datetime:
        return datetime.now(UTC)


class Uuid4Factory:
    def new(self) -> UUID:
        return uuid4()


def create_application() -> FastAPI:
    settings = ServiceSettings()
    database_url = settings.database_url.get_secret_value()
    engine = create_engine(database_url, pool_pre_ping=True)
    migrations = PostgresMigrationRunner(engine)
    opportunities = SqlAlchemyOpportunityRepository(engine)
    discovery = SqlAlchemyDiscoveryRepository(engine)
    capture_manual = CaptureManualOpportunity(
        opportunities,
        SystemClock(),
        Uuid4Factory(),
    )
    token_verifier = JwtTokenVerifier(
        SigningKeyResolver(str(settings.oidc_jwks_url)),
        str(settings.oidc_issuer),
        settings.oidc_audience,
    )
    sources = tuple(
        SourceDefinition(
            source_type="ats_api",
            provider="greenhouse",
            source_id=board.source_id,
            employer=board.employer,
        )
        for board in settings.greenhouse_boards
    )
    board_tokens = {board.source_id: board.board_token for board in settings.greenhouse_boards}
    web_client = httpx2.Client(timeout=5, trust_env=False)
    web = (
        OpenWebGateway(
            web_client, str(settings.ai_gateway_url), settings.ai_gateway_token.get_secret_value()
        )
        if settings.ai_gateway_url is not None and settings.ai_gateway_token is not None
        else None
    )
    registry = DiscoverySources(sources, GreenhouseGateway(UrlLibTransport(), board_tokens), web)

    def shutdown() -> None:
        web_client.close()
        engine.dispose()

    return create_http_app(
        StartService(migrations),
        capture_manual=capture_manual,
        query_catalog=QueryOpportunityCatalog(opportunities),
        synchronize_search=SynchronizeSearchSnapshot(discovery),
        run_discovery=RunDailyDiscovery(
            discovery,
            registry,
            registry,
            Uuid4Factory(),
        ),
        token_verifier=token_verifier,
        shutdown=shutdown,
    )
