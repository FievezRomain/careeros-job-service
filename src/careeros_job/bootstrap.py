from datetime import UTC, datetime
from uuid import UUID, uuid4

from fastapi import FastAPI
from pydantic import AnyHttpUrl, BaseModel, ConfigDict, Field
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
    GreenhouseSourceRegistry,
    UrlLibTransport,
)
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
    return create_http_app(
        StartService(migrations),
        capture_manual=capture_manual,
        query_catalog=QueryOpportunityCatalog(opportunities),
        synchronize_search=SynchronizeSearchSnapshot(discovery),
        run_discovery=RunDailyDiscovery(
            discovery,
            GreenhouseSourceRegistry(sources),
            GreenhouseGateway(UrlLibTransport(), board_tokens),
            Uuid4Factory(),
        ),
        token_verifier=token_verifier,
        shutdown=engine.dispose,
    )
