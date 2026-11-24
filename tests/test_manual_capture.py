import hashlib
import json
import os
from datetime import UTC, datetime, timedelta
from typing import cast
from uuid import UUID

import jwt
import pytest
from cryptography.hazmat.primitives.asymmetric import rsa
from fastapi import FastAPI
from fastapi.testclient import TestClient
from jwt import PyJWK
from pydantic import ValidationError
from sqlalchemy import create_engine

from careeros_job.adapters.http import JwtTokenVerifier, create_http_app
from careeros_job.adapters.postgresql.migrations.runner import PostgresMigrationRunner
from careeros_job.adapters.postgresql.repository import SqlAlchemyOpportunityRepository
from careeros_job.application.manual_capture import (
    AccountContext,
    CaptureManualOpportunity,
    CaptureResult,
    QueryOpportunityCatalog,
    TokenValidationError,
)
from careeros_job.application.startup import MigrationState, StartService
from careeros_job.domain.opportunity import (
    ManualOpportunityDraft,
    Opportunity,
    OpportunityOwner,
    SourceProvenance,
)

ACCOUNT_ID = UUID("30000000-0000-0000-0000-000000000001")
TENANT_ID = UUID("10000000-0000-0000-0000-000000000001")
OPPORTUNITY_ID = UUID("40000000-0000-4000-8000-000000000001")
NOW = datetime(2026, 11, 24, 8, tzinfo=UTC)


class CompleteMigrations:
    state = MigrationState(expected=("0001", "0002", "0003"), applied=("0001", "0002", "0003"))

    def apply_pending(self) -> MigrationState:
        return self.state

    def inspect(self) -> MigrationState:
        return self.state


class FixedClock:
    def now(self) -> datetime:
        return NOW


class FixedIds:
    def new(self) -> UUID:
        return OPPORTUNITY_ID


class SequenceIds:
    def __init__(self) -> None:
        self._value = 0

    def new(self) -> UUID:
        self._value += 1
        return UUID(f"40000000-0000-4000-8000-{self._value:012d}")


class MemoryRepository:
    def __init__(self) -> None:
        self.commands: dict[tuple[UUID, UUID, str], tuple[str, UUID]] = {}
        self.records: list[tuple[OpportunityOwner, str, str, Opportunity]] = []
        self.last_owner: OpportunityOwner | None = None

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
        self.last_owner = owner
        key = (owner.tenant_id, owner.account_id, idempotency_key)
        existing = self.commands.get(key)
        if existing is not None:
            fingerprint, existing_id = existing
            if fingerprint != request_fingerprint:
                return CaptureResult(outcome="conflict")
            opportunity = self.get(owner, existing_id)
            return CaptureResult(outcome="replayed", opportunity=opportunity)
        group_records = [
            record
            for record_owner, record_grouping, _, record in self.records
            if record_owner == owner and record_grouping == grouping_key
        ]
        duplicate = next(
            (
                record
                for record_owner, record_grouping, record_content, record in self.records
                if record_owner == owner
                and record_grouping == grouping_key
                and record_content == content_fingerprint
            ),
            None,
        )
        if duplicate is not None:
            self.commands[key] = (request_fingerprint, duplicate.opportunity_id)
            return CaptureResult(outcome="deduplicated", opportunity=duplicate)
        opportunity = Opportunity(
            opportunity_id=opportunity_id,
            group_id=group_id,
            version=len(group_records) + 1,
            title=draft.title,
            employer=draft.employer,
            description=draft.description,
            source=SourceProvenance(
                reference=draft.source_reference,
                raw_text=draft.raw_source,
                captured_at=captured_at,
            ),
            created_at=captured_at,
        )
        self.records.append((owner, grouping_key, content_fingerprint, opportunity))
        self.commands[key] = (request_fingerprint, opportunity_id)
        return CaptureResult(outcome="created", opportunity=opportunity)

    def get(self, owner: OpportunityOwner, opportunity_id: UUID) -> Opportunity | None:
        return next(
            (
                record
                for record_owner, _, _, record in self.records
                if record_owner == owner and record.opportunity_id == opportunity_id
            ),
            None,
        )

    def list_current(
        self, owner: OpportunityOwner, *, offset: int, limit: int
    ) -> tuple[tuple[Opportunity, ...], int]:
        latest: dict[UUID, Opportunity] = {}
        for record_owner, _, _, record in self.records:
            if record_owner != owner:
                continue
            current = latest.get(record.group_id)
            if current is None or current.version < record.version:
                latest[record.group_id] = record
        ordered = sorted(latest.values(), key=lambda item: (item.created_at, item.opportunity_id))
        ordered.reverse()
        return tuple(ordered[offset : offset + limit]), len(ordered)


class StaticVerifier:
    def __init__(
        self,
        scopes: tuple[str, ...] = ("jobs:read", "jobs:write"),
        account_id: UUID = ACCOUNT_ID,
    ) -> None:
        self._scopes = scopes
        self._account_id = account_id

    def verify(self, token: str) -> AccountContext:
        if token != "valid-token":
            raise TokenValidationError
        return AccountContext(account_id=self._account_id, tenant_id=TENANT_ID, scopes=self._scopes)


def draft() -> dict[str, str]:
    return {
        "title": "Platform Engineer",
        "employer": "Example Labs",
        "description": "Build a synthetic platform.",
        "sourceReference": "https://jobs.example.invalid/platform-engineer",
        "rawSource": "Platform Engineer\n  Exact source wording.  ",
    }


def application(
    repository: MemoryRepository | None = None,
    verifier: StaticVerifier | None = None,
    identifiers: FixedIds | SequenceIds | None = None,
) -> tuple[TestClient, MemoryRepository]:
    storage = repository or MemoryRepository()
    use_case = CaptureManualOpportunity(storage, FixedClock(), identifiers or FixedIds())
    app = create_http_app(
        StartService(CompleteMigrations()),
        capture_manual=use_case,
        query_catalog=QueryOpportunityCatalog(storage),
        token_verifier=verifier or StaticVerifier(),
    )
    return TestClient(app), storage


def headers(key: str = "manual-capture-0001") -> dict[str, str]:
    return {"Authorization": "Bearer valid-token", "Idempotency-Key": key}


def test_manual_capture_preserves_provenance_and_owner_with_idempotent_replay() -> None:
    client, repository = application()
    with client:
        created = client.post("/v1/jobs/manual", json=draft(), headers=headers())
        replayed = client.post("/v1/jobs/manual", json=draft(), headers=headers())

    assert created.status_code == 201
    assert replayed.status_code == 200
    assert created.json() == replayed.json()
    assert created.json()["source"] == {
        "kind": "manual",
        "reference": draft()["sourceReference"],
        "rawText": draft()["rawSource"],
        "capturedAt": "2026-11-24T08:00:00Z",
    }
    assert repository.last_owner == OpportunityOwner(account_id=ACCOUNT_ID, tenant_id=TENANT_ID)
    legacy_payload = json.dumps(draft(), sort_keys=True, separators=(",", ":"))
    assert repository.commands[(TENANT_ID, ACCOUNT_ID, "manual-capture-0001")][0] == (
        hashlib.sha256(legacy_payload.encode()).hexdigest()
    )


def test_validation_authentication_scope_and_idempotency_fail_closed() -> None:
    client, _ = application()
    with client:
        invalid = client.post(
            "/v1/jobs/manual", json={**draft(), "title": ""}, headers=headers("invalid-input-001")
        )
        forged = client.post(
            "/v1/jobs/manual",
            json={**draft(), "ownerId": str(UUID(int=9))},
            headers=headers("forged-owner-001"),
        )
        missing_token = client.post(
            "/v1/jobs/manual", json=draft(), headers={"Idempotency-Key": "missing-token-001"}
        )
        invalid_token = client.post(
            "/v1/jobs/manual",
            json=draft(),
            headers={"Authorization": "Bearer invalid", "Idempotency-Key": "bad-token-000001"},
        )
    denied_client, _ = application(verifier=StaticVerifier(scopes=()))
    with denied_client:
        denied = denied_client.post(
            "/v1/jobs/manual", json=draft(), headers=headers("missing-scope-001")
        )

    assert invalid.status_code == 422
    assert forged.status_code == 422
    assert missing_token.status_code == 401
    assert invalid_token.status_code == 401
    assert denied.status_code == 403
    assert denied.json()["code"] == "FORBIDDEN"


def test_reusing_an_idempotency_key_for_another_request_is_a_conflict() -> None:
    client, _ = application()
    with client:
        assert client.post("/v1/jobs/manual", json=draft(), headers=headers()).status_code == 201
        conflict = client.post(
            "/v1/jobs/manual",
            json={**draft(), "title": "Different role"},
            headers=headers(),
        )
    assert conflict.status_code == 409
    assert conflict.json()["code"] == "IDEMPOTENCY_KEY_REUSED"


def test_manual_capture_contract_is_versioned_and_rejects_blank_raw_source() -> None:
    client, _ = application()
    with client:
        contract = cast(FastAPI, client.app).openapi()
        invalid = client.post(
            "/v1/jobs/manual",
            json={**draft(), "rawSource": "   "},
            headers=headers("blank-source-0001"),
        )
    assert "/v1/jobs/manual" in contract["paths"]
    assert "/v1/jobs" in contract["paths"]
    assert "/v1/jobs/{opportunity_id}" in contract["paths"]
    assert set(contract["paths"]["/v1/jobs/manual"]["post"]["responses"]) >= {
        "200",
        "201",
        "400",
        "401",
        "403",
        "409",
        "422",
    }
    assert invalid.status_code == 422
    assert set(contract["paths"]["/v1/jobs"]["get"]["responses"]) >= {
        "200",
        "401",
        "403",
        "422",
    }
    assert set(contract["paths"]["/v1/jobs/{opportunity_id}"]["get"]["responses"]) >= {
        "200",
        "401",
        "403",
        "404",
        "422",
    }
    with pytest.raises(ValidationError):
        ManualOpportunityDraft.model_validate({**draft(), "rawSource": "\n"})


def test_normalization_deduplication_versioning_and_current_catalog_query() -> None:
    client, _ = application(identifiers=SequenceIds())
    with client:
        first = client.post(
            "/v1/jobs/manual",
            json={**draft(), "title": "  Platform\u00a0Engineer  ", "employer": "Example   Labs"},
            headers=headers("normalized-first-001"),
        )
        duplicate = client.post(
            "/v1/jobs/manual",
            json={**draft(), "title": "platform engineer", "employer": "example labs"},
            headers=headers("normalized-second-01"),
        )
        deduplicated_key_conflict = client.post(
            "/v1/jobs/manual",
            json={**draft(), "description": "A different request."},
            headers=headers("normalized-second-01"),
        )
        changed = client.post(
            "/v1/jobs/manual",
            json={
                **draft(),
                "description": "Build a synthetic platform and own reliability.",
                "rawSource": f"{draft()['rawSource']}\nOwn reliability.",
            },
            headers=headers("material-change-0001"),
        )
        separated = client.post(
            "/v1/jobs/manual",
            json={**draft(), "forceSeparate": True},
            headers=headers("forced-separate-001"),
        )
        catalog = client.get("/v1/jobs?page=1&pageSize=20", headers=headers())
        previous = client.get(f"/v1/jobs/{first.json()['opportunityId']}", headers=headers())

    assert first.status_code == 201
    assert first.json()["title"] == "Platform Engineer"
    assert first.json()["employer"] == "Example Labs"
    assert duplicate.status_code == 200
    assert duplicate.json()["opportunityId"] == first.json()["opportunityId"]
    assert deduplicated_key_conflict.status_code == 409
    assert changed.status_code == 201
    assert changed.json()["groupId"] == first.json()["groupId"]
    assert changed.json()["version"] == 2
    assert previous.status_code == 200
    assert previous.json()["version"] == 1
    assert separated.status_code == 201
    assert separated.json()["groupId"] != first.json()["groupId"]
    assert catalog.status_code == 200
    assert catalog.json()["total"] == 2
    assert catalog.json()["items"][0]["evidence"] == {
        "statedFields": ["title", "employer", "description"],
        "inferredFields": [],
        "unknownFields": ["location", "contractType", "workMode"],
    }


def test_catalog_query_requires_read_scope_and_hides_cross_owner_records() -> None:
    repository = MemoryRepository()
    identifiers = SequenceIds()
    owner_client, _ = application(repository=repository, identifiers=identifiers)
    other_id = UUID("30000000-0000-0000-0000-000000000002")
    other_client, _ = application(
        repository=repository,
        verifier=StaticVerifier(account_id=other_id),
        identifiers=identifiers,
    )
    denied_client, _ = application(
        repository=repository,
        verifier=StaticVerifier(scopes=("jobs:write",)),
        identifiers=identifiers,
    )
    with owner_client, other_client, denied_client:
        created = owner_client.post(
            "/v1/jobs/manual", json=draft(), headers=headers("owned-query-00001")
        )
        opportunity_id = created.json()["opportunityId"]
        hidden = other_client.get(f"/v1/jobs/{opportunity_id}", headers=headers())
        denied = denied_client.get("/v1/jobs?page=1&pageSize=20", headers=headers())
        unauthenticated = owner_client.get("/v1/jobs?page=1&pageSize=20")

    assert hidden.status_code == 404
    assert hidden.json()["code"] == "OPPORTUNITY_NOT_FOUND"
    assert denied.status_code == 403
    assert unauthenticated.status_code == 401


@pytest.mark.skipif(not os.getenv("TEST_DATABASE_URL"), reason="isolated PostgreSQL unavailable")
def test_postgresql_repository_persists_and_replays_owned_manual_capture() -> None:
    database_url = os.environ["TEST_DATABASE_URL"]
    engine = create_engine(database_url, pool_pre_ping=True)
    migrations = PostgresMigrationRunner(engine)
    try:
        migrations.rollback_all()
        assert migrations.apply_pending().is_complete
        use_case = CaptureManualOpportunity(
            SqlAlchemyOpportunityRepository(engine),
            FixedClock(),
            SequenceIds(),
        )
        query = QueryOpportunityCatalog(SqlAlchemyOpportunityRepository(engine))
        account = AccountContext(
            account_id=ACCOUNT_ID,
            tenant_id=TENANT_ID,
            scopes=("jobs:read", "jobs:write"),
        )
        manual_draft = ManualOpportunityDraft.model_validate(draft())

        created = use_case.execute(account, manual_draft, "postgres-capture-0001")
        replayed = use_case.execute(account, manual_draft, "postgres-capture-0001")
        deduplicated = use_case.execute(
            account,
            manual_draft.model_copy(update={"title": "platform engineer"}),
            "postgres-capture-0002",
        )
        changed = use_case.execute(
            account,
            manual_draft.model_copy(
                update={
                    "description": "Build a synthetic platform and own reliability.",
                    "raw_source": f"{draft()['rawSource']}\nOwn reliability.",
                }
            ),
            "postgres-capture-0003",
        )
        catalog = query.list(account, page=1, page_size=20)

        assert created.created
        assert not replayed.created
        assert not deduplicated.created
        assert replayed.opportunity == created.opportunity
        assert replayed.opportunity.source.raw_text == draft()["rawSource"]
        assert changed.opportunity.group_id == created.opportunity.group_id
        assert changed.opportunity.version == 2
        assert catalog.total == 1
        assert catalog.items == (changed.opportunity,)
        assert query.get(account, created.opportunity.opportunity_id).version == 1
    finally:
        migrations.rollback_all()
        migrations.dispose()


class StaticResolver:
    def __init__(self, key: PyJWK) -> None:
        self._key = key

    def resolve(self, token: str) -> PyJWK:
        return self._key


def test_oidc_adapter_validates_signature_audience_and_scopes() -> None:
    private_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    public_jwk = PyJWK.from_json(jwt.algorithms.RSAAlgorithm.to_jwk(private_key.public_key()))
    verifier = JwtTokenVerifier(
        StaticResolver(public_jwk),
        "https://identity.example.invalid/realms/careeros",
        "careeros-api",
    )
    now = datetime.now(UTC)
    claims = {
        "aud": "careeros-api",
        "exp": now + timedelta(minutes=5),
        "iat": now,
        "iss": "https://identity.example.invalid/realms/careeros",
        "scope": "jobs:write jobs:write",
        "sub": str(ACCOUNT_ID),
        "tenant_id": str(TENANT_ID),
    }
    token = jwt.encode(claims, private_key, algorithm="RS256", headers={"kid": "synthetic"})

    assert verifier.verify(token) == AccountContext(
        account_id=ACCOUNT_ID, tenant_id=TENANT_ID, scopes=("jobs:write",)
    )
    invalid = jwt.encode(
        {**claims, "aud": "another-api"},
        private_key,
        algorithm="RS256",
        headers={"kid": "synthetic"},
    )
    with pytest.raises(TokenValidationError):
        verifier.verify(invalid)
