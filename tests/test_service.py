import json
import logging
import os
from collections.abc import Iterator

import pytest
from fastapi.testclient import TestClient
from pydantic import ValidationError
from sqlalchemy import create_engine, text

from careeros_job.adapters.http import JsonFormatter, create_http_app
from careeros_job.adapters.postgresql.migrations.runner import (
    DatabaseSettings,
    PostgresMigrationRunner,
)
from careeros_job.application.startup import (
    MigrationHeadsIncomplete,
    MigrationState,
    StartService,
)
from careeros_job.domain.health import ServiceState


class FakeMigrationRunner:
    def __init__(self, state: MigrationState) -> None:
        self.state = state
        self.apply_calls = 0
        self.inspect_calls = 0

    def apply_pending(self) -> MigrationState:
        self.apply_calls += 1
        return self.state

    def inspect(self) -> MigrationState:
        self.inspect_calls += 1
        return self.state


def complete_migrations() -> MigrationState:
    return MigrationState(expected=("0001",), applied=("0001",))


def test_startup_applies_and_verifies_migrations_before_ready() -> None:
    migrations = FakeMigrationRunner(complete_migrations())
    service = StartService(migrations)

    status = service.execute()

    assert migrations.apply_calls == 1
    assert migrations.inspect_calls == 1
    assert status.state is ServiceState.READY
    assert status.migration_heads == ("0001",)


def test_startup_refuses_incomplete_migration_heads() -> None:
    migrations = FakeMigrationRunner(MigrationState(expected=("0001",), applied=()))

    with pytest.raises(MigrationHeadsIncomplete):
        StartService(migrations).execute()


def test_database_settings_require_postgresql_and_redact_credentials() -> None:
    with pytest.raises(ValidationError):
        DatabaseSettings(database_url="sqlite+pysqlite:///:memory:")

    settings = DatabaseSettings(
        database_url="postgresql+psycopg://service:synthetic-password@localhost/jobs"
    )

    assert "synthetic-password" not in repr(settings)


@pytest.fixture
def request_logger() -> Iterator[logging.Logger]:
    logger = logging.getLogger("careeros_job.test.requests")
    logger.handlers.clear()
    handler = logging.StreamHandler()
    handler.setFormatter(JsonFormatter())
    logger.addHandler(handler)
    logger.setLevel(logging.INFO)
    logger.propagate = True
    yield logger
    logger.handlers.clear()


def test_health_contract_and_correlation_propagation(request_logger: logging.Logger) -> None:
    service = StartService(FakeMigrationRunner(complete_migrations()))
    app = create_http_app(service, request_logger)

    with TestClient(app) as client:
        response = client.get("/health/ready", headers={"X-Correlation-ID": "synthetic-123"})

    assert response.status_code == 200
    assert response.json() == {"service": "careeros-job-service", "state": "ready"}
    assert response.headers["X-Correlation-ID"] == "synthetic-123"


def test_logs_exclude_request_payload_and_reject_unsafe_correlation_id(
    request_logger: logging.Logger, caplog: pytest.LogCaptureFixture
) -> None:
    service = StartService(FakeMigrationRunner(complete_migrations()))
    app = create_http_app(service, request_logger)

    with caplog.at_level(logging.INFO), TestClient(app) as client:
        response = client.post(
            "/missing",
            content='{"token":"synthetic-sensitive-value"}',
            headers={"X-Correlation-ID": "unsafe value\nforged"},
        )

    assert response.status_code == 404
    assert response.headers["X-Correlation-ID"] != "unsafe value\nforged"
    assert "synthetic-sensitive-value" not in caplog.text
    request_record = next(record for record in caplog.records if record.name == request_logger.name)
    structured_log = json.loads(JsonFormatter().format(request_record))
    assert structured_log["correlation_id"] == response.headers["X-Correlation-ID"]
    assert structured_log["path"] == "/missing"
    assert "synthetic-sensitive-value" not in structured_log.values()


def test_http_startup_fails_closed_when_migrations_are_incomplete() -> None:
    service = StartService(FakeMigrationRunner(MigrationState(expected=("0001",), applied=())))
    app = create_http_app(service)

    with pytest.raises(MigrationHeadsIncomplete), TestClient(app):
        pass


@pytest.mark.skipif(not os.getenv("TEST_DATABASE_URL"), reason="isolated PostgreSQL unavailable")
def test_postgresql_migration_upgrade_verification_and_rollback() -> None:
    database_url = os.environ["TEST_DATABASE_URL"]
    runner = PostgresMigrationRunner.from_url(database_url)
    inspection_engine = create_engine(database_url)
    try:
        runner.rollback_all()
        upgraded = runner.apply_pending()
        with inspection_engine.connect() as connection:
            table_name = connection.scalar(text("SELECT to_regclass('public.job_records')"))

        assert upgraded.is_complete
        assert table_name == "job_records"

        downgraded = runner.rollback_all()
        with inspection_engine.connect() as connection:
            table_name = connection.scalar(text("SELECT to_regclass('public.job_records')"))

        assert downgraded.applied == ()
        assert table_name is None
    finally:
        inspection_engine.dispose()
        runner.dispose()
