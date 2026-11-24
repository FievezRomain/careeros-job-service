from datetime import UTC, date, datetime
from typing import Literal, cast
from uuid import UUID

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from careeros_job.adapters.http import create_http_app
from careeros_job.adapters.sources.greenhouse import (
    GreenhouseGateway,
    TransportResponse,
)
from careeros_job.application.discovery import (
    DiscoveredOpportunity,
    DiscoveryRunCommand,
    DiscoveryRunResult,
    ObservationResult,
    RunDailyDiscovery,
    SearchProjection,
    SearchSchedule,
    SearchSignature,
    SearchSnapshotEvent,
    SourceCheckpoint,
    SourceCollection,
    SourceDefinition,
    SynchronizeSearchSnapshot,
)
from careeros_job.application.manual_capture import (
    AccountContext,
    PermissionDenied,
    TokenValidationError,
)
from careeros_job.application.startup import MigrationState, StartService
from careeros_job.domain.opportunity import (
    DiscoveredSourceProvenance,
    Opportunity,
    OpportunityEvidence,
    OpportunityOwner,
)

TENANT_ID = UUID("10000000-0000-4000-8000-000000000001")
ACCOUNT_A = UUID("20000000-0000-4000-8000-000000000001")
ACCOUNT_B = UUID("20000000-0000-4000-8000-000000000002")
CRITERIA_A = UUID("30000000-0000-4000-8000-000000000001")
SOURCE = SourceDefinition(
    source_id="greenhouse:example",
    source_type="ats_api",
    provider="greenhouse",
    employer="Example Labs",
)
SIGNATURE = SearchSignature(
    signature_id="a" * 64,
    target_roles=("python engineer",),
    locations=("paris",),
    remote_policy="remote",
    contract_types=("cdi",),
)


class CompleteMigrations:
    def apply_pending(self) -> MigrationState:
        return MigrationState(expected=("0004",), applied=("0004",))

    def inspect(self) -> MigrationState:
        return self.apply_pending()


class SequenceIds:
    def __init__(self) -> None:
        self.value = 0

    def new(self) -> UUID:
        self.value += 1
        return UUID(int=self.value)


class MemoryDiscoveryRepository:
    def __init__(self) -> None:
        self.searches: dict[tuple[UUID, UUID], SearchProjection] = {}
        self.runs: dict[str, tuple[str, str]] = {}
        self.checkpoints: dict[tuple[str, str], SourceCheckpoint] = {}
        self.observations: dict[tuple[UUID, UUID, str, str], tuple[str, Opportunity]] = {}
        self.events: list[object] = []

    def synchronize(self, event: SearchSnapshotEvent) -> bool:
        key = (event.tenant_id, event.account_id)
        current = self.searches.get(key)
        if current is not None and current.occurred_at >= event.occurred_at:
            return False
        self.searches[key] = SearchProjection(
            event_id=event.event_id,
            occurred_at=event.occurred_at,
            owner=OpportunityOwner(account_id=event.account_id, tenant_id=event.tenant_id),
            criteria_version_id=event.criteria_version_id,
            state=event.state,
            schedule=event.schedule,
            search_signature=event.search_signature,
        )
        return True

    def current_searches(self) -> tuple[SearchProjection, ...]:
        return tuple(self.searches.values())

    def mark_ran(self, projection: SearchProjection, local_date: date) -> None:
        key = (projection.owner.tenant_id, projection.owner.account_id)
        self.searches[key] = projection.model_copy(update={"last_run_local_date": local_date})

    def claim_run(
        self, idempotency_key: str, fingerprint: str
    ) -> Literal["started", "replayed", "conflict"]:
        current = self.runs.get(idempotency_key)
        if current is None or current[1] == "failed":
            self.runs[idempotency_key] = (fingerprint, "started")
            return "started"
        return "replayed" if current[0] == fingerprint else "conflict"

    def complete_run(self, idempotency_key: str, result: DiscoveryRunResult) -> None:
        self.runs[idempotency_key] = (self.runs[idempotency_key][0], "completed")

    def fail_run(self, idempotency_key: str) -> None:
        self.runs[idempotency_key] = (self.runs[idempotency_key][0], "failed")

    def checkpoint(self, source_id: str, signature_id: str) -> SourceCheckpoint:
        return self.checkpoints.get((source_id, signature_id), SourceCheckpoint())

    def save_checkpoint(
        self, source_id: str, signature_id: str, checkpoint: SourceCheckpoint
    ) -> None:
        self.checkpoints[(source_id, signature_id)] = checkpoint

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
        del grouping_key
        key = (owner.tenant_id, owner.account_id, source.source_id, candidate.external_id)
        previous = self.observations.get(key)
        if previous is not None and previous[0] == material_hash:
            opportunity = previous[1].model_copy(
                update={
                    "source": previous[1].source.model_copy(update={"last_seen_at": observed_at})
                }
            )
            self.observations[key] = (material_hash, opportunity)
            return ObservationResult(outcome="unchanged", opportunity=opportunity)
        version = previous[1].version + 1 if previous else 1
        opportunity = Opportunity(
            opportunity_id=opportunity_id,
            group_id=previous[1].group_id if previous else group_id,
            version=version,
            title=candidate.title,
            employer=candidate.employer,
            description=candidate.description,
            location=candidate.location,
            source=DiscoveredSourceProvenance(
                kind="greenhouse",
                source_id=source.source_id,
                external_identifier=candidate.external_id,
                reference=candidate.source_url,
                raw_text=candidate.raw_source,
                observed_at=observed_at,
                last_seen_at=observed_at,
            ),
            evidence=OpportunityEvidence(
                stated_fields=("title", "employer", "description", "location"),
                unknown_fields=("contractType", "workMode"),
            ),
            created_at=observed_at,
        )
        self.observations[key] = (material_hash, opportunity)
        self.events.append(
            {
                "eventId": event_id,
                "correlationId": correlation_id,
                "criteriaVersionId": criteria_version_id,
                "opportunityId": opportunity.opportunity_id,
                "version": opportunity.version,
            }
        )
        return ObservationResult(
            outcome="changed" if previous else "created", opportunity=opportunity
        )


class StaticRegistry:
    def sources(self, signature: SearchSignature) -> tuple[SourceDefinition, ...]:
        assert signature == SIGNATURE
        return (SOURCE,)


class MutableGateway:
    def __init__(self) -> None:
        self.description = "Build a synthetic service."
        self.calls = 0

    def collect(
        self,
        source: SourceDefinition,
        signature: SearchSignature,
        checkpoint: SourceCheckpoint,
    ) -> SourceCollection:
        self.calls += 1
        assert source == SOURCE
        assert signature == SIGNATURE
        return SourceCollection(
            items=(
                DiscoveredOpportunity(
                    external_id="100",
                    title="Python Engineer",
                    employer="Example Labs",
                    description=self.description,
                    location="Paris",
                    source_url="https://boards.example.invalid/jobs/100",
                    raw_source=f"Python Engineer\n{self.description}",
                    observed_at=datetime(2026, 11, 24, 8, 30, tzinfo=UTC),
                ),
            ),
            pages_consumed=2,
            reported_total=1,
            checkpoint=SourceCheckpoint(etag=f"etag-{self.calls}"),
            rate_limit_remaining=99,
        )


class FailingOnceGateway(MutableGateway):
    def collect(
        self,
        source: SourceDefinition,
        signature: SearchSignature,
        checkpoint: SourceCheckpoint,
    ) -> SourceCollection:
        if self.calls == 0:
            self.calls += 1
            raise TimeoutError("synthetic provider timeout")
        return super().collect(source, signature, checkpoint)


def event(
    account_id: UUID, *, state: Literal["active", "paused"] = "active", minute: int = 0
) -> SearchSnapshotEvent:
    return SearchSnapshotEvent(
        event_id=UUID(int=account_id.int + minute + 1),
        event_type="profile.match-relevant-change.v1",
        schema_version="1.0",
        occurred_at=datetime(2026, 11, 24, 7, minute, tzinfo=UTC),
        correlation_id="profile-change-0001",
        idempotency_key=f"profile-search-{account_id}",
        tenant_id=TENANT_ID,
        account_id=account_id,
        changed_fields=("searchCriteria",),
        criteria_version_id=UUID(int=CRITERIA_A.int + account_id.int % 10),
        state=state,
        schedule=SearchSchedule(timezone="UTC", window_start="08:00", window_end="10:00"),
        search_signature=SIGNATURE,
    )


def account(*scopes: str) -> AccountContext:
    return AccountContext(account_id=ACCOUNT_A, tenant_id=TENANT_ID, scopes=scopes)


def test_daily_discovery_groups_signatures_and_versions_only_material_changes() -> None:
    repository = MemoryDiscoveryRepository()
    sync = SynchronizeSearchSnapshot(repository)
    assert sync.execute(account("discovery:write"), event(ACCOUNT_A))
    assert sync.execute(account("discovery:write"), event(ACCOUNT_B))
    assert sync.execute(account("discovery:write"), event(UUID(int=99), state="paused"))
    gateway = MutableGateway()
    run = RunDailyDiscovery(repository, StaticRegistry(), gateway, SequenceIds())

    first = run.execute(
        account("discovery:run"),
        DiscoveryRunCommand(scheduled_at="2026-11-24T08:30:00Z"),
        idempotency_key="daily-run-20261124-0830",
        correlation_id="daily-run-20261124",
    )
    replay = run.execute(
        account("discovery:run"),
        DiscoveryRunCommand(scheduled_at="2026-11-24T08:30:00Z"),
        idempotency_key="daily-run-20261124-0830",
        correlation_id="daily-run-20261124",
    )

    assert first.eligible_searches == 2
    assert first.signature_groups == 1
    assert first.sources_visited == 1
    assert first.pages_consumed == 2
    assert first.created == 2
    assert len(repository.events) == 2
    assert replay.replayed

    unchanged = run.execute(
        account("discovery:run"),
        DiscoveryRunCommand(scheduled_at="2026-11-25T08:30:00Z"),
        idempotency_key="daily-run-20261125-0830",
        correlation_id="daily-run-20261125",
    )
    assert unchanged.unchanged == 2
    assert len(repository.events) == 2

    gateway.description = "Build a synthetic service and own reliability."
    changed = run.execute(
        account("discovery:run"),
        DiscoveryRunCommand(scheduled_at="2026-11-26T08:30:00Z"),
        idempotency_key="daily-run-20261126-0830",
        correlation_id="daily-run-20261126",
    )
    assert changed.changed == 2
    assert len(repository.events) == 4
    assert all(value[1].version == 2 for value in repository.observations.values())


def test_discovery_use_cases_require_internal_scopes() -> None:
    repository = MemoryDiscoveryRepository()
    with pytest.raises(PermissionDenied):
        SynchronizeSearchSnapshot(repository).execute(account(), event(ACCOUNT_A))
    with pytest.raises(PermissionDenied):
        RunDailyDiscovery(repository, StaticRegistry(), MutableGateway(), SequenceIds()).execute(
            account(),
            DiscoveryRunCommand(scheduled_at="2026-11-24T08:30:00Z"),
            idempotency_key="daily-run-20261124-0830",
            correlation_id="daily-run-20261124",
        )


def test_failed_provider_run_is_retryable_with_the_same_idempotency_key() -> None:
    repository = MemoryDiscoveryRepository()
    SynchronizeSearchSnapshot(repository).execute(account("discovery:write"), event(ACCOUNT_A))
    run = RunDailyDiscovery(repository, StaticRegistry(), FailingOnceGateway(), SequenceIds())
    command = DiscoveryRunCommand(scheduled_at="2026-11-24T08:30:00Z")

    with pytest.raises(TimeoutError, match="synthetic provider timeout"):
        run.execute(
            account("discovery:run"),
            command,
            idempotency_key="daily-run-retry-000001",
            correlation_id="daily-run-retry",
        )

    retried = run.execute(
        account("discovery:run"),
        command,
        idempotency_key="daily-run-retry-000001",
        correlation_id="daily-run-retry",
    )
    assert retried.created == 1


class StaticVerifier:
    def verify(self, token: str) -> AccountContext:
        if token != "automation-token":
            raise TokenValidationError
        return account("discovery:write", "discovery:run")


def test_internal_http_contract_accepts_profile_events_and_idempotent_runs() -> None:
    repository = MemoryDiscoveryRepository()
    app = create_http_app(
        StartService(CompleteMigrations()),
        capture_manual=object(),  # type: ignore[arg-type]
        synchronize_search=SynchronizeSearchSnapshot(repository),
        run_discovery=RunDailyDiscovery(
            repository, StaticRegistry(), MutableGateway(), SequenceIds()
        ),
        token_verifier=StaticVerifier(),
    )
    headers = {"Authorization": "Bearer automation-token"}
    with TestClient(app) as client:
        synchronized = client.post(
            "/v1/internal/discovery/search-snapshots",
            json=event(ACCOUNT_A).model_dump(mode="json", by_alias=True),
            headers=headers,
        )
        run = client.post(
            "/v1/internal/discovery/runs",
            json={"scheduledAt": "2026-11-24T08:30:00Z"},
            headers={**headers, "Idempotency-Key": "daily-run-20261124-0830"},
        )
        unauthenticated = client.post(
            "/v1/internal/discovery/runs",
            json={"scheduledAt": "2026-11-24T08:30:00Z"},
            headers={"Idempotency-Key": "daily-run-unauth-0001"},
        )
        contract = cast(FastAPI, client.app).openapi()

    assert synchronized.status_code == 200
    assert synchronized.json() == {"applied": True}
    assert run.status_code == 200
    assert run.json()["created"] == 1
    assert unauthenticated.status_code == 401
    assert "/v1/internal/discovery/search-snapshots" in contract["paths"]
    assert "/v1/internal/discovery/runs" in contract["paths"]


class FakeTransport:
    def __init__(self) -> None:
        self.headers: dict[str, str] = {}

    def get(self, url: str, headers: dict[str, str]) -> TransportResponse:
        assert url.endswith("/v1/boards/example/jobs?content=true")
        self.headers = headers
        return TransportResponse(
            status=200,
            headers={"etag": "next-etag", "x-ratelimit-remaining": "42"},
            body=(
                b'{"jobs":[{"id":100,"title":"Python Engineer",'
                b'"absolute_url":"https://boards.example.invalid/jobs/100",'
                b'"location":{"name":"Paris"},"content":"<p>Build safely.</p>"}],'
                b'"meta":{"total":1}}'
            ),
        )


def test_greenhouse_adapter_uses_conditional_requests_and_checks_completeness() -> None:
    transport = FakeTransport()
    gateway = GreenhouseGateway(transport, {SOURCE.source_id: "example"})
    collection = gateway.collect(SOURCE, SIGNATURE, SourceCheckpoint(etag="previous-etag"))

    assert transport.headers["If-None-Match"] == "previous-etag"
    assert collection.pages_consumed == 1
    assert not collection.truncated
    assert collection.rate_limit_remaining == 42
    assert collection.checkpoint.etag == "next-etag"
    assert collection.items[0].description == "Build safely."
    assert collection.items[0].raw_source.startswith('{"absolute_url"')
