from typing import Protocol

from pydantic import BaseModel, ConfigDict

from careeros_job.domain.health import ServiceState


class MigrationState(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    expected: tuple[str, ...]
    applied: tuple[str, ...]

    @property
    def is_complete(self) -> bool:
        return self.applied == self.expected


class MigrationRunner(Protocol):
    def apply_pending(self) -> MigrationState: ...

    def inspect(self) -> MigrationState: ...


class StartupStatus(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    state: ServiceState
    migration_heads: tuple[str, ...] = ()


class MigrationHeadsIncomplete(RuntimeError):
    pass


class StartService:
    def __init__(self, migrations: MigrationRunner) -> None:
        self._migrations = migrations
        self._status = StartupStatus(state=ServiceState.STARTING)

    @property
    def status(self) -> StartupStatus:
        return self._status

    def execute(self) -> StartupStatus:
        self._migrations.apply_pending()
        migration_state = self._migrations.inspect()
        if not migration_state.is_complete:
            raise MigrationHeadsIncomplete(
                "database migration heads do not match the service release"
            )
        self._status = StartupStatus(
            state=ServiceState.READY,
            migration_heads=migration_state.applied,
        )
        return self._status
