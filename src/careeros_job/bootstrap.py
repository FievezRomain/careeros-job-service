from datetime import UTC, datetime
from uuid import UUID, uuid4

from fastapi import FastAPI
from pydantic import AnyHttpUrl, Field
from pydantic_settings import SettingsConfigDict
from sqlalchemy import create_engine

from careeros_job.adapters.http import JwtTokenVerifier, SigningKeyResolver, create_http_app
from careeros_job.adapters.postgresql.migrations.runner import (
    DatabaseSettings,
    PostgresMigrationRunner,
)
from careeros_job.adapters.postgresql.repository import SqlAlchemyOpportunityRepository
from careeros_job.application.manual_capture import (
    CaptureManualOpportunity,
    QueryOpportunityCatalog,
)
from careeros_job.application.startup import StartService


class ServiceSettings(DatabaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore", frozen=True)

    oidc_issuer: AnyHttpUrl
    oidc_audience: str = Field(min_length=1)
    oidc_jwks_url: AnyHttpUrl


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
    return create_http_app(
        StartService(migrations),
        capture_manual=capture_manual,
        query_catalog=QueryOpportunityCatalog(opportunities),
        token_verifier=token_verifier,
        shutdown=engine.dispose,
    )
