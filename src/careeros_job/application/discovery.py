"""Daily, provider-neutral opportunity discovery use cases and core-owned ports."""

import hashlib
import json
from datetime import date, datetime, time
from typing import Literal, Protocol
from uuid import UUID
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from careeros_job.application.manual_capture import AccountContext, PermissionDenied
from careeros_job.domain.opportunity import Opportunity, OpportunityOwner


def _camel(value: str) -> str:
    first, *rest = value.split("_")
    return first + "".join(part.title() for part in rest)


class DiscoveryModel(BaseModel):
    model_config = ConfigDict(
        alias_generator=_camel,
        extra="forbid",
        frozen=True,
        populate_by_name=True,
        serialize_by_alias=True,
    )


class SearchSchedule(DiscoveryModel):
    timezone: str
    window_start: str = Field(pattern=r"^(?:[01]\d|2[0-3]):[0-5]\d$")
    window_end: str = Field(pattern=r"^(?:[01]\d|2[0-3]):[0-5]\d$")

    @field_validator("timezone")
    @classmethod
    def validate_timezone(cls, value: str) -> str:
        try:
            ZoneInfo(value)
        except ZoneInfoNotFoundError as error:
            raise ValueError("timezone must be an IANA timezone") from error
        return value

    @model_validator(mode="after")
    def validate_window(self) -> SearchSchedule:
        if self.window_start >= self.window_end:
            raise ValueError("windowStart must precede windowEnd")
        return self


class SearchSignature(DiscoveryModel):
    signature_id: str = Field(min_length=64, max_length=64)
    target_roles: tuple[str, ...] = Field(min_length=1)
    locations: tuple[str, ...] = Field(min_length=1)
    remote_policy: Literal["remote", "hybrid", "onsite", "any"]
    contract_types: tuple[str, ...] = Field(min_length=1)
    exclusions: tuple[str, ...] = ()


class SearchSnapshotEvent(DiscoveryModel):
    event_id: UUID
    event_type: Literal["profile.match-relevant-change.v1"]
    schema_version: Literal["1.0"]
    occurred_at: datetime
    correlation_id: str = Field(min_length=1, max_length=128)
    idempotency_key: str = Field(min_length=1, max_length=200)
    tenant_id: UUID
    account_id: UUID
    changed_fields: tuple[str, ...]
    criteria_version_id: UUID
    state: Literal["active", "paused"]
    schedule: SearchSchedule
    search_signature: SearchSignature


class SearchProjection(DiscoveryModel):
    event_id: UUID
    occurred_at: datetime
    owner: OpportunityOwner
    criteria_version_id: UUID
    state: Literal["active", "paused"]
    schedule: SearchSchedule
    search_signature: SearchSignature
    last_run_local_date: date | None = None


class DiscoveryRunCommand(DiscoveryModel):
    scheduled_at: datetime

    @field_validator("scheduled_at")
    @classmethod
    def require_aware_datetime(cls, value: datetime) -> datetime:
        if value.tzinfo is None:
            raise ValueError("scheduledAt must include a timezone")
        return value


class SourceDefinition(DiscoveryModel):
    source_id: str = Field(min_length=1, max_length=200)
    source_type: Literal["ats_api", "public_feed", "public_dataset", "career_page"]
    provider: str = Field(min_length=1, max_length=100)
    employer: str = Field(min_length=1, max_length=200)


class SourceCheckpoint(DiscoveryModel):
    etag: str | None = None
    last_modified: str | None = None
    cursor: str | None = None


class DiscoveredOpportunity(DiscoveryModel):
    external_id: str = Field(min_length=1, max_length=500)
    title: str = Field(min_length=1, max_length=200)
    employer: str = Field(min_length=1, max_length=200)
    description: str = Field(min_length=1, max_length=10_000)
    location: str = Field(min_length=1, max_length=500)
    source_url: str = Field(min_length=1, max_length=2_000)
    raw_source: str = Field(min_length=1, max_length=100_000)
    observed_at: datetime


class SourceCollection(DiscoveryModel):
    items: tuple[DiscoveredOpportunity, ...] = ()
    pages_consumed: int = Field(ge=0)
    reported_total: int | None = Field(default=None, ge=0)
    truncated: bool = False
    not_modified: bool = False
    checkpoint: SourceCheckpoint = Field(default_factory=SourceCheckpoint)
    rate_limit_remaining: int | None = Field(default=None, ge=0)


class ObservationResult(DiscoveryModel):
    outcome: Literal["created", "changed", "unchanged"]
    opportunity: Opportunity


class DiscoveredVersionEvent(DiscoveryModel):
    event_id: UUID
    event_type: Literal["job.opportunity-version-discovered.v1"] = (
        "job.opportunity-version-discovered.v1"
    )
    schema_version: Literal["1.0"] = "1.0"
    occurred_at: datetime
    correlation_id: str = Field(min_length=1, max_length=128)
    tenant_id: UUID
    account_id: UUID
    criteria_version_id: UUID
    opportunity_id: UUID
    opportunity_version: int = Field(ge=1)
    source_id: str
    source_external_id: str


class DiscoveryRunResult(DiscoveryModel):
    replayed: bool = False
    eligible_searches: int = 0
    signature_groups: int = 0
    sources_visited: int = 0
    pages_consumed: int = 0
    created: int = 0
    changed: int = 0
    unchanged: int = 0
    truncated_sources: tuple[str, ...] = ()


class DiscoveryIdempotencyConflict(Exception):
    pass


class SearchProjectionRepository(Protocol):
    def synchronize(self, event: SearchSnapshotEvent) -> bool: ...

    def current_searches(self) -> tuple[SearchProjection, ...]: ...

    def mark_ran(self, projection: SearchProjection, local_date: date) -> None: ...

    def claim_run(
        self, idempotency_key: str, fingerprint: str
    ) -> Literal["started", "replayed", "conflict"]: ...

    def complete_run(self, idempotency_key: str, result: DiscoveryRunResult) -> None: ...

    def fail_run(self, idempotency_key: str) -> None: ...

    def checkpoint(self, source_id: str, signature_id: str) -> SourceCheckpoint: ...

    def save_checkpoint(
        self, source_id: str, signature_id: str, checkpoint: SourceCheckpoint
    ) -> None: ...

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
    ) -> ObservationResult: ...


class SourceRegistry(Protocol):
    def sources(self, signature: SearchSignature) -> tuple[SourceDefinition, ...]: ...


class SourceGateway(Protocol):
    def collect(
        self,
        source: SourceDefinition,
        signature: SearchSignature,
        checkpoint: SourceCheckpoint,
    ) -> SourceCollection: ...


class IdentifierFactory(Protocol):
    def new(self) -> UUID: ...


def _fingerprint(value: object) -> str:
    return hashlib.sha256(
        json.dumps(value, sort_keys=True, separators=(",", ":"), default=str).encode()
    ).hexdigest()


def _eligible(projection: SearchProjection, scheduled_at: datetime) -> tuple[bool, date]:
    local = scheduled_at.astimezone(ZoneInfo(projection.schedule.timezone))
    local_time = local.time().replace(second=0, microsecond=0, tzinfo=None)
    start = time.fromisoformat(projection.schedule.window_start)
    end = time.fromisoformat(projection.schedule.window_end)
    local_date = local.date()
    return (
        projection.state == "active"
        and start <= local_time < end
        and projection.last_run_local_date != local_date,
        local_date,
    )


class SynchronizeSearchSnapshot:
    def __init__(self, repository: SearchProjectionRepository) -> None:
        self._repository = repository

    def execute(self, account: AccountContext, event: SearchSnapshotEvent) -> bool:
        if "discovery:write" not in account.scopes:
            raise PermissionDenied
        return self._repository.synchronize(event)


class RunDailyDiscovery:
    def __init__(
        self,
        repository: SearchProjectionRepository,
        registry: SourceRegistry,
        gateway: SourceGateway,
        identifiers: IdentifierFactory,
    ) -> None:
        self._repository = repository
        self._registry = registry
        self._gateway = gateway
        self._identifiers = identifiers

    def execute(
        self,
        account: AccountContext,
        command: DiscoveryRunCommand,
        *,
        idempotency_key: str,
        correlation_id: str,
    ) -> DiscoveryRunResult:
        if "discovery:run" not in account.scopes:
            raise PermissionDenied
        fingerprint = _fingerprint(command.model_dump(mode="json"))
        claim = self._repository.claim_run(idempotency_key, fingerprint)
        if claim == "conflict":
            raise DiscoveryIdempotencyConflict
        if claim == "replayed":
            return DiscoveryRunResult(replayed=True)

        try:
            eligible: list[tuple[SearchProjection, date]] = []
            for projection in self._repository.current_searches():
                is_eligible, local_date = _eligible(projection, command.scheduled_at)
                if is_eligible:
                    eligible.append((projection, local_date))
            groups: dict[str, list[tuple[SearchProjection, date]]] = {}
            for item in eligible:
                groups.setdefault(item[0].search_signature.signature_id, []).append(item)

            counts = {"sources": 0, "pages": 0, "created": 0, "changed": 0, "unchanged": 0}
            truncated: list[str] = []
            for grouped in groups.values():
                signature = grouped[0][0].search_signature
                for source in self._registry.sources(signature):
                    checkpoint = self._repository.checkpoint(
                        source.source_id, signature.signature_id
                    )
                    collection = self._gateway.collect(source, signature, checkpoint)
                    counts["sources"] += 1
                    counts["pages"] += collection.pages_consumed
                    if collection.truncated:
                        truncated.append(source.source_id)
                    if not collection.not_modified:
                        for projection, _ in grouped:
                            self._observe_collection(
                                projection,
                                source,
                                collection,
                                correlation_id,
                                counts,
                            )
                    self._repository.save_checkpoint(
                        source.source_id, signature.signature_id, collection.checkpoint
                    )
                for projection, local_date in grouped:
                    self._repository.mark_ran(projection, local_date)

            result = DiscoveryRunResult(
                eligible_searches=len(eligible),
                signature_groups=len(groups),
                sources_visited=counts["sources"],
                pages_consumed=counts["pages"],
                created=counts["created"],
                changed=counts["changed"],
                unchanged=counts["unchanged"],
                truncated_sources=tuple(sorted(set(truncated))),
            )
            self._repository.complete_run(idempotency_key, result)
            return result
        except Exception:
            self._repository.fail_run(idempotency_key)
            raise

    def _observe_collection(
        self,
        projection: SearchProjection,
        source: SourceDefinition,
        collection: SourceCollection,
        correlation_id: str,
        counts: dict[str, int],
    ) -> None:
        for candidate in collection.items:
            grouping_key = _fingerprint([candidate.title.casefold(), candidate.employer.casefold()])
            group_id = UUID(
                _fingerprint(
                    [
                        str(projection.owner.tenant_id),
                        str(projection.owner.account_id),
                        grouping_key,
                    ]
                )[:32]
            )
            material_hash = _fingerprint(
                [
                    candidate.title.casefold(),
                    candidate.employer.casefold(),
                    candidate.description,
                    candidate.location,
                    candidate.raw_source,
                ]
            )
            observation = self._repository.observe_and_enqueue(
                projection.owner,
                source,
                candidate,
                opportunity_id=self._identifiers.new(),
                group_id=group_id,
                material_hash=material_hash,
                grouping_key=grouping_key,
                observed_at=candidate.observed_at,
                event_id=self._identifiers.new(),
                correlation_id=correlation_id,
                criteria_version_id=projection.criteria_version_id,
            )
            counts[observation.outcome] += 1
