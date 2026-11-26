"""PostgreSQL projection, observation and outbox adapter for daily discovery."""

import hashlib
from datetime import UTC, date, datetime
from typing import Literal
from uuid import UUID

from sqlalchemy import JSON, Date, DateTime, Engine, String, Uuid, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Mapped, mapped_column, sessionmaker

from careeros_job.adapters.postgresql.repository import Base, JobRecordRow, _opportunity
from careeros_job.application.discovery import (
    DiscoveredOpportunity,
    DiscoveredVersionEvent,
    DiscoveryRunResult,
    ObservationResult,
    SearchProjection,
    SearchSchedule,
    SearchSignature,
    SearchSnapshotEvent,
    SourceCheckpoint,
    SourceDefinition,
)
from careeros_job.domain.opportunity import OpportunityOwner


class SearchProjectionRow(Base):
    __tablename__ = "discovery_search_projections"

    tenant_id: Mapped[str] = mapped_column(String(36), primary_key=True)
    account_id: Mapped[str] = mapped_column(String(36), primary_key=True)
    event_id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), unique=True, nullable=False)
    occurred_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    criteria_version_id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    state: Mapped[str] = mapped_column(String(16), nullable=False)
    schedule_json: Mapped[dict[str, object]] = mapped_column(JSON, nullable=False)
    signature_json: Mapped[dict[str, object]] = mapped_column(JSON, nullable=False)
    last_run_local_date: Mapped[date | None] = mapped_column(Date, nullable=True)


class DiscoveryRunRow(Base):
    __tablename__ = "discovery_runs"

    idempotency_key: Mapped[str] = mapped_column(String(128), primary_key=True)
    request_fingerprint: Mapped[str] = mapped_column(String(64), nullable=False)
    state: Mapped[str] = mapped_column(String(16), nullable=False)
    result_json: Mapped[dict[str, object] | None] = mapped_column(JSON, nullable=True)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class SourceCheckpointRow(Base):
    __tablename__ = "discovery_source_checkpoints"

    source_id: Mapped[str] = mapped_column(String(200), primary_key=True)
    signature_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    etag: Mapped[str | None] = mapped_column(String(500), nullable=True)
    last_modified: Mapped[str | None] = mapped_column(String(500), nullable=True)
    cursor: Mapped[str | None] = mapped_column(String(2_000), nullable=True)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class DiscoveryObservationRow(Base):
    __tablename__ = "discovery_observations"

    tenant_id: Mapped[str] = mapped_column(String(36), primary_key=True)
    account_id: Mapped[str] = mapped_column(String(36), primary_key=True)
    source_id: Mapped[str] = mapped_column(String(200), primary_key=True)
    external_id: Mapped[str] = mapped_column(String(500), primary_key=True)
    material_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    current_opportunity_id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    last_seen_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class DiscoveryOutboxRow(Base):
    __tablename__ = "discovery_outbox"

    event_id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True)
    event_type: Mapped[str] = mapped_column(String(120), nullable=False)
    schema_version: Mapped[str] = mapped_column(String(16), nullable=False)
    occurred_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    correlation_id: Mapped[str] = mapped_column(String(128), nullable=False)
    payload: Mapped[dict[str, object]] = mapped_column(JSON, nullable=False)
    published_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


def _aware(value: datetime) -> datetime:
    return value.replace(tzinfo=value.tzinfo or UTC)


def _projection(row: SearchProjectionRow) -> SearchProjection:
    return SearchProjection(
        event_id=row.event_id,
        occurred_at=_aware(row.occurred_at),
        owner=OpportunityOwner(account_id=UUID(row.account_id), tenant_id=UUID(row.tenant_id)),
        criteria_version_id=row.criteria_version_id,
        state=row.state,
        schedule=SearchSchedule.model_validate(row.schedule_json),
        search_signature=SearchSignature.model_validate(row.signature_json),
        last_run_local_date=row.last_run_local_date,
    )


class SqlAlchemyDiscoveryRepository:
    def __init__(self, engine: Engine) -> None:
        self._sessions = sessionmaker(engine, expire_on_commit=False)

    def synchronize(self, event: SearchSnapshotEvent) -> bool:
        key = {"tenant_id": str(event.tenant_id), "account_id": str(event.account_id)}
        with self._sessions.begin() as session:
            row = session.get(SearchProjectionRow, key)
            if row is not None and _aware(row.occurred_at) >= event.occurred_at:
                return False
            if row is None:
                row = SearchProjectionRow(**key)
                session.add(row)
            row.event_id = event.event_id
            row.occurred_at = event.occurred_at
            row.criteria_version_id = event.criteria_version_id
            row.state = event.state
            row.schedule_json = event.schedule.model_dump(mode="json", by_alias=True)
            row.signature_json = event.search_signature.model_dump(mode="json", by_alias=True)
            row.last_run_local_date = None
            return True

    def current_searches(self) -> tuple[SearchProjection, ...]:
        with self._sessions() as session:
            rows = session.scalars(select(SearchProjectionRow)).all()
            return tuple(_projection(row) for row in rows)

    def mark_ran(self, projection: SearchProjection, local_date: date) -> None:
        with self._sessions.begin() as session:
            row = session.get(
                SearchProjectionRow,
                {
                    "tenant_id": str(projection.owner.tenant_id),
                    "account_id": str(projection.owner.account_id),
                },
            )
            if row is not None and row.event_id == projection.event_id:
                row.last_run_local_date = local_date

    def claim_run(
        self, idempotency_key: str, fingerprint: str
    ) -> Literal["started", "replayed", "conflict"]:
        try:
            with self._sessions.begin() as session:
                row = session.get(DiscoveryRunRow, idempotency_key)
                if row is not None:
                    return self._claim_existing(row, fingerprint)
                session.add(
                    DiscoveryRunRow(
                        idempotency_key=idempotency_key,
                        request_fingerprint=fingerprint,
                        state="started",
                        updated_at=datetime.now(UTC),
                    )
                )
                return "started"
        except IntegrityError:
            with self._sessions.begin() as session:
                row = session.get(DiscoveryRunRow, idempotency_key)
                if row is None:
                    raise
                return self._claim_existing(row, fingerprint)

    @staticmethod
    def _claim_existing(
        row: DiscoveryRunRow, fingerprint: str
    ) -> Literal["started", "replayed", "conflict"]:
        if row.request_fingerprint != fingerprint:
            return "conflict"
        if row.state in {"started", "completed"}:
            return "replayed"
        row.state = "started"
        row.updated_at = datetime.now(UTC)
        return "started"

    def complete_run(self, idempotency_key: str, result: DiscoveryRunResult) -> None:
        with self._sessions.begin() as session:
            row = session.get(DiscoveryRunRow, idempotency_key)
            if row is None:
                raise RuntimeError("discovery run is missing")
            row.state = "completed"
            row.result_json = result.model_dump(mode="json", by_alias=True)
            row.updated_at = datetime.now(UTC)

    def fail_run(self, idempotency_key: str) -> None:
        with self._sessions.begin() as session:
            row = session.get(DiscoveryRunRow, idempotency_key)
            if row is not None:
                row.state = "failed"
                row.updated_at = datetime.now(UTC)

    def checkpoint(self, source_id: str, signature_id: str) -> SourceCheckpoint:
        with self._sessions() as session:
            row = session.get(
                SourceCheckpointRow, {"source_id": source_id, "signature_id": signature_id}
            )
            return (
                SourceCheckpoint(etag=row.etag, last_modified=row.last_modified, cursor=row.cursor)
                if row is not None
                else SourceCheckpoint()
            )

    def save_checkpoint(
        self, source_id: str, signature_id: str, checkpoint: SourceCheckpoint
    ) -> None:
        with self._sessions.begin() as session:
            row = session.get(
                SourceCheckpointRow, {"source_id": source_id, "signature_id": signature_id}
            )
            if row is None:
                row = SourceCheckpointRow(source_id=source_id, signature_id=signature_id)
                session.add(row)
            row.etag = checkpoint.etag
            row.last_modified = checkpoint.last_modified
            row.cursor = checkpoint.cursor
            row.updated_at = datetime.now(UTC)

    def observe_and_enqueue(
        self,
        owner: OpportunityOwner,
        source: SourceDefinition,
        candidate: DiscoveredOpportunity,
        *,
        opportunity_id: UUID,
        group_id: UUID,
        material_hash: str,
        grouping_key: str,
        observed_at: datetime,
        event_id: UUID,
        correlation_id: str,
        criteria_version_id: UUID,
    ) -> ObservationResult:
        key = {
            "tenant_id": str(owner.tenant_id),
            "account_id": str(owner.account_id),
            "source_id": source.source_id,
            "external_id": candidate.external_id,
        }
        with self._sessions.begin() as session:
            observation = session.get(DiscoveryObservationRow, key)
            previous = (
                session.get(JobRecordRow, observation.current_opportunity_id)
                if observation is not None
                else None
            )
            if observation is not None and (
                observation.material_hash == material_hash
                or (
                    previous is not None
                    and previous.source_kind == "open_web"
                    and previous.description is not None
                    and candidate.source_access == "external_to_verify"
                )
            ):
                if previous is None:
                    raise RuntimeError("observation references a missing opportunity")
                observation.last_seen_at = observed_at
                previous.last_seen_at = observed_at
                previous.updated_at = observed_at
                return ObservationResult(outcome="unchanged", opportunity=_opportunity(previous))

            version = (previous.version + 1) if previous is not None else 1
            effective_group = previous.group_id if previous is not None else group_id
            command_key = hashlib.sha256(
                f"{source.source_id}:{candidate.external_id}:{version}".encode()
            ).hexdigest()
            row = JobRecordRow(
                id=opportunity_id,
                owner_id=str(owner.account_id),
                tenant_id=str(owner.tenant_id),
                group_id=effective_group,
                version=version,
                grouping_key=grouping_key,
                content_fingerprint=material_hash,
                title=candidate.title,
                employer=candidate.employer,
                description=candidate.description,
                source_kind=source.provider,
                source_reference=candidate.source_url,
                raw_source=candidate.raw_source,
                captured_at=observed_at,
                idempotency_key=command_key,
                request_fingerprint=material_hash,
                created_at=observed_at,
                updated_at=observed_at,
                source_id=source.source_id,
                source_external_id=candidate.external_id,
                source_location=candidate.location,
                source_original_url=candidate.original_url,
                source_retrieval_provider=candidate.retrieval_provider,
                last_seen_at=observed_at,
                status="active",
            )
            session.add(row)
            session.flush()
            if observation is None:
                observation = DiscoveryObservationRow(**key)
                session.add(observation)
            observation.material_hash = material_hash
            observation.current_opportunity_id = opportunity_id
            observation.last_seen_at = observed_at
            event = DiscoveredVersionEvent(
                event_id=event_id,
                occurred_at=observed_at,
                correlation_id=correlation_id,
                tenant_id=owner.tenant_id,
                account_id=owner.account_id,
                criteria_version_id=criteria_version_id,
                opportunity_id=row.id,
                opportunity_version=row.version,
                source_id=source.source_id,
                source_external_id=candidate.external_id,
            )
            session.add(
                DiscoveryOutboxRow(
                    event_id=event.event_id,
                    event_type=event.event_type,
                    schema_version=event.schema_version,
                    occurred_at=event.occurred_at,
                    correlation_id=event.correlation_id,
                    payload=event.model_dump(mode="json", by_alias=True),
                )
            )
            return ObservationResult(
                outcome="changed" if previous is not None else "created",
                opportunity=_opportunity(row),
            )
