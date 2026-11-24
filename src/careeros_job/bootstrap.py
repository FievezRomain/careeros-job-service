from fastapi import FastAPI

from careeros_job.adapters.http import create_http_app
from careeros_job.adapters.postgresql.migrations.runner import (
    DatabaseSettings,
    PostgresMigrationRunner,
)
from careeros_job.application.startup import StartService


def create_application() -> FastAPI:
    settings = DatabaseSettings()
    migrations = PostgresMigrationRunner.from_url(settings.database_url.get_secret_value())
    return create_http_app(StartService(migrations))
