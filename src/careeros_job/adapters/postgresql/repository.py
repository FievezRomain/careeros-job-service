from datetime import UTC, datetime
from uuid import UUID

from sqlalchemy import DateTime, Engine, String, Text, UniqueConstraint, Uuid, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import DeclarativeBase, Mapped, Session, mapped_column, sessionmaker

from careeros_job.application.manual_capture import CaptureResult
from careeros_job.domain.opportunity import (
    ManualOpportunityDraft,
    Opportunity,
    OpportunityOwner,
    SourceProvenance,
)


class Base(DeclarativeBase):
    pass


class JobRecordRow(Base):
    __tablename__ = "job_records"
    __table_args__ = (
        UniqueConstraint(
            "tenant_id", "owner_id", "idempotency_key", name="uq_job_owner_idempotency"
        ),
    )

    id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True)
    owner_id: Mapped[str] = mapped_column(String(36), nullable=False)
    tenant_id: Mapped[str] = mapped_column(String(36), nullable=False)
    title: Mapped[str] = mapped_column(String(200), nullable=False)
    employer: Mapped[str] = mapped_column(String(200), nullable=False)
    description: Mapped[str] = mapped_column(String(10_000), nullable=False)
    source_kind: Mapped[str] = mapped_column(String(32), nullable=False)
    source_reference: Mapped[str] = mapped_column(String(2_000), nullable=False)
    raw_source: Mapped[str] = mapped_column(Text, nullable=False)
    captured_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    idempotency_key: Mapped[str] = mapped_column(String(128), nullable=False)
    request_fingerprint: Mapped[str] = mapped_column(String(64), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


def _aware(value: datetime) -> datetime:
    return value.replace(tzinfo=value.tzinfo or UTC)


def _opportunity(row: JobRecordRow) -> Opportunity:
    return Opportunity(
        opportunity_id=row.id,
        title=row.title,
        employer=row.employer,
        description=row.description,
        source=SourceProvenance(
            reference=row.source_reference,
            raw_text=row.raw_source,
            captured_at=_aware(row.captured_at),
        ),
        created_at=_aware(row.created_at),
    )


def _owned_command(
    session: Session, owner: OpportunityOwner, idempotency_key: str
) -> JobRecordRow | None:
    return session.scalar(
        select(JobRecordRow).where(
            JobRecordRow.tenant_id == str(owner.tenant_id),
            JobRecordRow.owner_id == str(owner.account_id),
            JobRecordRow.idempotency_key == idempotency_key,
        )
    )


def _existing_result(row: JobRecordRow, request_fingerprint: str) -> CaptureResult:
    if row.request_fingerprint != request_fingerprint:
        return CaptureResult(outcome="conflict")
    return CaptureResult(outcome="replayed", opportunity=_opportunity(row))


class SqlAlchemyOpportunityRepository:
    def __init__(self, engine: Engine) -> None:
        self._sessions = sessionmaker(engine, expire_on_commit=False)

    def capture(
        self,
        owner: OpportunityOwner,
        draft: ManualOpportunityDraft,
        *,
        opportunity_id: UUID,
        captured_at: datetime,
        idempotency_key: str,
        request_fingerprint: str,
    ) -> CaptureResult:
        try:
            return self._capture(
                owner,
                draft,
                opportunity_id=opportunity_id,
                captured_at=captured_at,
                idempotency_key=idempotency_key,
                request_fingerprint=request_fingerprint,
            )
        except IntegrityError:
            with self._sessions() as session:
                existing = _owned_command(session, owner, idempotency_key)
                if existing is None:
                    raise RuntimeError("concurrent capture outcome was not persisted") from None
                return _existing_result(existing, request_fingerprint)

    def _capture(
        self,
        owner: OpportunityOwner,
        draft: ManualOpportunityDraft,
        *,
        opportunity_id: UUID,
        captured_at: datetime,
        idempotency_key: str,
        request_fingerprint: str,
    ) -> CaptureResult:
        with self._sessions.begin() as session:
            existing = _owned_command(session, owner, idempotency_key)
            if existing is not None:
                return _existing_result(existing, request_fingerprint)
            row = JobRecordRow(
                id=opportunity_id,
                owner_id=str(owner.account_id),
                tenant_id=str(owner.tenant_id),
                title=draft.title,
                employer=draft.employer,
                description=draft.description,
                source_kind="manual",
                source_reference=draft.source_reference,
                raw_source=draft.raw_source,
                captured_at=captured_at,
                idempotency_key=idempotency_key,
                request_fingerprint=request_fingerprint,
                created_at=captured_at,
                updated_at=captured_at,
            )
            session.add(row)
        return CaptureResult(outcome="created", opportunity=_opportunity(row))
