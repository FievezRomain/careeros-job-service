from datetime import UTC, datetime
from uuid import UUID

from sqlalchemy import (
    DateTime,
    Engine,
    ForeignKey,
    Integer,
    String,
    Text,
    UniqueConstraint,
    Uuid,
    and_,
    func,
    select,
)
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import DeclarativeBase, Mapped, Session, mapped_column, sessionmaker

from careeros_job.application.manual_capture import CaptureResult
from careeros_job.domain.opportunity import (
    DiscoveredSourceProvenance,
    ManualOpportunityDraft,
    Opportunity,
    OpportunityEvidence,
    OpportunityOwner,
    SourceProvenance,
    WebSourceProvenance,
)


class Base(DeclarativeBase):
    pass


class JobRecordRow(Base):
    __tablename__ = "job_records"
    __table_args__ = (
        UniqueConstraint(
            "tenant_id", "owner_id", "idempotency_key", name="uq_job_owner_idempotency"
        ),
        UniqueConstraint(
            "tenant_id",
            "owner_id",
            "grouping_key",
            "content_fingerprint",
            name="uq_job_group_content",
        ),
        UniqueConstraint(
            "tenant_id", "owner_id", "group_id", "version", name="uq_job_group_version"
        ),
    )

    id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True)
    owner_id: Mapped[str] = mapped_column(String(36), nullable=False)
    tenant_id: Mapped[str] = mapped_column(String(36), nullable=False)
    group_id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    version: Mapped[int] = mapped_column(Integer, nullable=False)
    grouping_key: Mapped[str] = mapped_column(String(64), nullable=False)
    content_fingerprint: Mapped[str] = mapped_column(String(64), nullable=False)
    title: Mapped[str] = mapped_column(String(200), nullable=False)
    employer: Mapped[str | None] = mapped_column(String(200), nullable=True)
    description: Mapped[str | None] = mapped_column(String(10_000), nullable=True)
    source_kind: Mapped[str] = mapped_column(String(32), nullable=False)
    source_reference: Mapped[str] = mapped_column(String(2_000), nullable=False)
    raw_source: Mapped[str] = mapped_column(Text, nullable=False)
    captured_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    idempotency_key: Mapped[str] = mapped_column(String(128), nullable=False)
    request_fingerprint: Mapped[str] = mapped_column(String(64), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    source_id: Mapped[str | None] = mapped_column(String(200), nullable=True)
    source_external_id: Mapped[str | None] = mapped_column(String(500), nullable=True)
    source_location: Mapped[str | None] = mapped_column(String(500), nullable=True)
    source_original_url: Mapped[str | None] = mapped_column(String(2_000), nullable=True)
    source_retrieval_provider: Mapped[str | None] = mapped_column(String(60), nullable=True)
    last_seen_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    status: Mapped[str] = mapped_column(String(32), nullable=False, default="active")


class JobCaptureCommandRow(Base):
    __tablename__ = "job_capture_commands"

    tenant_id: Mapped[str] = mapped_column(String(36), primary_key=True)
    owner_id: Mapped[str] = mapped_column(String(36), primary_key=True)
    idempotency_key: Mapped[str] = mapped_column(String(128), primary_key=True)
    request_fingerprint: Mapped[str] = mapped_column(String(64), nullable=False)
    opportunity_id: Mapped[UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("job_records.id"), nullable=False
    )


def _aware(value: datetime) -> datetime:
    return value.replace(tzinfo=value.tzinfo or UTC)


def _opportunity(row: JobRecordRow) -> Opportunity:
    source: SourceProvenance | DiscoveredSourceProvenance | WebSourceProvenance = (
        SourceProvenance(
            reference=row.source_reference,
            raw_text=row.raw_source,
            captured_at=_aware(row.captured_at),
        )
        if row.source_kind == "manual"
        else DiscoveredSourceProvenance(
            kind="greenhouse",
            source_id=row.source_id or row.source_kind,
            external_identifier=row.source_external_id or str(row.id),
            reference=row.source_reference,
            raw_text=row.raw_source,
            observed_at=_aware(row.captured_at),
            last_seen_at=_aware(row.last_seen_at or row.captured_at),
        )
    )
    source_access = "full_text"
    if row.source_kind == "open_web":
        source = WebSourceProvenance(
            source_id=row.source_id or row.source_kind,
            external_identifier=row.source_external_id or str(row.id),
            reference=row.source_reference,
            original_reference=row.source_original_url or row.source_reference,
            retrieval_provider=row.source_retrieval_provider,
            raw_text=row.raw_source,
            observed_at=_aware(row.captured_at),
            last_seen_at=_aware(row.last_seen_at or row.captured_at),
        )
        if row.description is None:
            source_access = "external_to_verify"
    fields = {
        "title": row.title,
        "employer": row.employer,
        "description": row.description,
        "location": row.source_location,
    }
    return Opportunity(
        opportunity_id=row.id,
        group_id=row.group_id,
        version=row.version,
        title=row.title,
        employer=row.employer,
        description=row.description,
        location=row.source_location,
        source=source,
        source_access=source_access,
        status=row.status,
        evidence=(
            OpportunityEvidence()
            if row.source_kind == "manual"
            else OpportunityEvidence(
                stated_fields=tuple(field for field, value in fields.items() if value is not None),
                unknown_fields=(
                    *[field for field, value in fields.items() if value is None],
                    "contractType",
                    "workMode",
                ),
            )
        ),
        created_at=_aware(row.created_at),
    )


def _owned_command(
    session: Session, owner: OpportunityOwner, idempotency_key: str
) -> tuple[JobCaptureCommandRow, JobRecordRow] | None:
    command = session.get(
        JobCaptureCommandRow,
        {
            "tenant_id": str(owner.tenant_id),
            "owner_id": str(owner.account_id),
            "idempotency_key": idempotency_key,
        },
    )
    if command is None:
        return None
    opportunity = session.get(JobRecordRow, command.opportunity_id)
    if opportunity is None:
        raise RuntimeError("capture command references a missing opportunity")
    return command, opportunity


def _existing_result(
    command: JobCaptureCommandRow, row: JobRecordRow, request_fingerprint: str
) -> CaptureResult:
    if command.request_fingerprint != request_fingerprint:
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
        group_id: UUID,
        captured_at: datetime,
        idempotency_key: str,
        request_fingerprint: str,
        grouping_key: str,
        content_fingerprint: str,
    ) -> CaptureResult:
        try:
            return self._capture(
                owner,
                draft,
                opportunity_id=opportunity_id,
                group_id=group_id,
                captured_at=captured_at,
                idempotency_key=idempotency_key,
                request_fingerprint=request_fingerprint,
                grouping_key=grouping_key,
                content_fingerprint=content_fingerprint,
            )
        except IntegrityError:
            with self._sessions.begin() as session:
                existing = _owned_command(session, owner, idempotency_key)
                if existing is not None:
                    return _existing_result(*existing, request_fingerprint)
                duplicate = self._content_version(session, owner, grouping_key, content_fingerprint)
                if duplicate is None:
                    pass
                else:
                    session.add(
                        JobCaptureCommandRow(
                            tenant_id=str(owner.tenant_id),
                            owner_id=str(owner.account_id),
                            idempotency_key=idempotency_key,
                            request_fingerprint=request_fingerprint,
                            opportunity_id=duplicate.id,
                        )
                    )
                    return CaptureResult(
                        outcome="deduplicated", opportunity=_opportunity(duplicate)
                    )
            return self._capture(
                owner,
                draft,
                opportunity_id=opportunity_id,
                group_id=group_id,
                captured_at=captured_at,
                idempotency_key=idempotency_key,
                request_fingerprint=request_fingerprint,
                grouping_key=grouping_key,
                content_fingerprint=content_fingerprint,
            )

    def _capture(
        self,
        owner: OpportunityOwner,
        draft: ManualOpportunityDraft,
        *,
        opportunity_id: UUID,
        group_id: UUID,
        captured_at: datetime,
        idempotency_key: str,
        request_fingerprint: str,
        grouping_key: str,
        content_fingerprint: str,
    ) -> CaptureResult:
        with self._sessions.begin() as session:
            existing = _owned_command(session, owner, idempotency_key)
            if existing is not None:
                return _existing_result(*existing, request_fingerprint)
            duplicate = self._content_version(session, owner, grouping_key, content_fingerprint)
            if duplicate is not None:
                session.add(
                    JobCaptureCommandRow(
                        tenant_id=str(owner.tenant_id),
                        owner_id=str(owner.account_id),
                        idempotency_key=idempotency_key,
                        request_fingerprint=request_fingerprint,
                        opportunity_id=duplicate.id,
                    )
                )
                return CaptureResult(outcome="deduplicated", opportunity=_opportunity(duplicate))
            latest = session.scalar(
                select(JobRecordRow)
                .where(
                    JobRecordRow.tenant_id == str(owner.tenant_id),
                    JobRecordRow.owner_id == str(owner.account_id),
                    JobRecordRow.grouping_key == grouping_key,
                )
                .order_by(JobRecordRow.version.desc())
                .limit(1)
            )
            row = JobRecordRow(
                id=opportunity_id,
                owner_id=str(owner.account_id),
                tenant_id=str(owner.tenant_id),
                group_id=group_id,
                version=(latest.version + 1) if latest is not None else 1,
                grouping_key=grouping_key,
                content_fingerprint=content_fingerprint,
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
            session.flush()
            session.add(
                JobCaptureCommandRow(
                    tenant_id=str(owner.tenant_id),
                    owner_id=str(owner.account_id),
                    idempotency_key=idempotency_key,
                    request_fingerprint=request_fingerprint,
                    opportunity_id=opportunity_id,
                )
            )
        return CaptureResult(outcome="created", opportunity=_opportunity(row))

    @staticmethod
    def _content_version(
        session: Session,
        owner: OpportunityOwner,
        grouping_key: str,
        content_fingerprint: str,
    ) -> JobRecordRow | None:
        return session.scalar(
            select(JobRecordRow).where(
                JobRecordRow.tenant_id == str(owner.tenant_id),
                JobRecordRow.owner_id == str(owner.account_id),
                JobRecordRow.grouping_key == grouping_key,
                JobRecordRow.content_fingerprint == content_fingerprint,
            )
        )

    def get(self, owner: OpportunityOwner, opportunity_id: UUID) -> Opportunity | None:
        with self._sessions() as session:
            row = session.scalar(
                select(JobRecordRow).where(
                    JobRecordRow.id == opportunity_id,
                    JobRecordRow.tenant_id == str(owner.tenant_id),
                    JobRecordRow.owner_id == str(owner.account_id),
                )
            )
            return _opportunity(row) if row is not None else None

    def list_current(
        self, owner: OpportunityOwner, *, offset: int, limit: int
    ) -> tuple[tuple[Opportunity, ...], int]:
        latest = (
            select(JobRecordRow.group_id, func.max(JobRecordRow.version).label("version"))
            .where(
                JobRecordRow.tenant_id == str(owner.tenant_id),
                JobRecordRow.owner_id == str(owner.account_id),
            )
            .group_by(JobRecordRow.group_id)
            .subquery()
        )
        with self._sessions() as session:
            rows = session.scalars(
                select(JobRecordRow)
                .join(
                    latest,
                    and_(
                        JobRecordRow.group_id == latest.c.group_id,
                        JobRecordRow.version == latest.c.version,
                    ),
                )
                .order_by(JobRecordRow.created_at.desc(), JobRecordRow.id.desc())
                .offset(offset)
                .limit(limit)
            ).all()
            total = session.scalar(select(func.count()).select_from(latest)) or 0
            return tuple(_opportunity(row) for row in rows), total
