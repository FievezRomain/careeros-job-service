import argparse
from pathlib import Path

from pydantic import BaseModel, ConfigDict, SecretStr, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict
from sqlalchemy import Engine, create_engine, text

from careeros_job.application.startup import MigrationState

_UP_MARKER = "-- migrate:up"
_DOWN_MARKER = "-- migrate:down"
_CREATE_LEDGER_SQL = (
    "CREATE TABLE IF NOT EXISTS careeros_job_schema_migrations "
    "(version VARCHAR(255) PRIMARY KEY, applied_at TIMESTAMPTZ NOT NULL DEFAULT NOW())"
)
_SELECT_APPLIED_SQL = text("SELECT version FROM careeros_job_schema_migrations ORDER BY version")
_INSERT_APPLIED_SQL = text("INSERT INTO careeros_job_schema_migrations (version) VALUES (:version)")
_DELETE_APPLIED_SQL = text("DELETE FROM careeros_job_schema_migrations WHERE version = :version")


class DatabaseSettings(BaseSettings):
    model_config = SettingsConfigDict(extra="ignore")

    database_url: SecretStr

    @field_validator("database_url")
    @classmethod
    def require_postgresql(cls, value: SecretStr) -> SecretStr:
        if not value.get_secret_value().startswith("postgresql+psycopg://"):
            raise ValueError("DATABASE_URL must use the PostgreSQL psycopg adapter")
        return value


class MigrationScript(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    version: str
    up_sql: str
    down_sql: str


class PostgresMigrationRunner:
    def __init__(self, engine: Engine, versions: Path | None = None) -> None:
        self._engine = engine
        self._versions = versions or Path(__file__).with_name("versions")

    @classmethod
    def from_url(cls, database_url: str) -> PostgresMigrationRunner:
        return cls(create_engine(database_url, pool_pre_ping=True))

    def _scripts(self) -> tuple[MigrationScript, ...]:
        scripts: list[MigrationScript] = []
        for path in sorted(self._versions.glob("*.sql")):
            source = path.read_text(encoding="utf-8")
            if _UP_MARKER not in source or _DOWN_MARKER not in source:
                raise ValueError(f"migration {path.name} must declare up and down sections")
            up_sql, down_sql = source.split(_DOWN_MARKER, maxsplit=1)
            scripts.append(
                MigrationScript(
                    version=path.stem,
                    up_sql=up_sql.split(_UP_MARKER, maxsplit=1)[1].strip(),
                    down_sql=down_sql.strip(),
                )
            )
        return tuple(scripts)

    def _ensure_ledger(self) -> None:
        with self._engine.begin() as connection:
            connection.exec_driver_sql(_CREATE_LEDGER_SQL)

    def _applied(self) -> tuple[str, ...]:
        self._ensure_ledger()
        with self._engine.connect() as connection:
            rows = connection.execute(_SELECT_APPLIED_SQL)
            return tuple(row[0] for row in rows)

    def inspect(self) -> MigrationState:
        return MigrationState(
            expected=tuple(script.version for script in self._scripts()),
            applied=self._applied(),
        )

    def apply_pending(self) -> MigrationState:
        applied = set(self._applied())
        for script in self._scripts():
            if script.version in applied:
                continue
            with self._engine.begin() as connection:
                connection.exec_driver_sql(script.up_sql)
                connection.execute(
                    _INSERT_APPLIED_SQL,
                    {"version": script.version},
                )
        return self.inspect()

    def rollback_all(self) -> MigrationState:
        applied = set(self._applied())
        for script in reversed(self._scripts()):
            if script.version not in applied:
                continue
            with self._engine.begin() as connection:
                connection.exec_driver_sql(script.down_sql)
                connection.execute(
                    _DELETE_APPLIED_SQL,
                    {"version": script.version},
                )
        return self.inspect()

    def dispose(self) -> None:
        self._engine.dispose()


def main() -> int:
    parser = argparse.ArgumentParser(description="Manage job-service PostgreSQL migrations")
    parser.add_argument("operation", choices=("upgrade", "downgrade"))
    operation = parser.parse_args().operation
    settings = DatabaseSettings()
    runner = PostgresMigrationRunner.from_url(settings.database_url.get_secret_value())
    try:
        state = runner.apply_pending() if operation == "upgrade" else runner.rollback_all()
        if operation == "upgrade" and not state.is_complete:
            return 1
        if operation == "downgrade" and state.applied:
            return 1
        return 0
    finally:
        runner.dispose()


if __name__ == "__main__":
    raise SystemExit(main())
