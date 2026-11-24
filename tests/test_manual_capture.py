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
    state = MigrationState(expected=("0001", "0002"), applied=("0001", "0002"))

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


class MemoryRepository:
    def __init__(self) -> None:
        self.records: dict[tuple[UUID, UUID, str], tuple[str, Opportunity]] = {}
        self.last_owner: OpportunityOwner | None = None

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
        self.last_owner = owner
        key = (owner.tenant_id, owner.account_id, idempotency_key)
        existing = self.records.get(key)
        if existing:
            fingerprint, opportunity = existing
            if fingerprint != request_fingerprint:
                return CaptureResult(outcome="conflict")
            return CaptureResult(outcome="replayed", opportunity=opportunity)
        opportunity = Opportunity(
            opportunity_id=opportunity_id,
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
        self.records[key] = (request_fingerprint, opportunity)
        return CaptureResult(outcome="created", opportunity=opportunity)


class StaticVerifier:
    def __init__(self, scopes: tuple[str, ...] = ("jobs:write",)) -> None:
        self._scopes = scopes

    def verify(self, token: str) -> AccountContext:
        if token != "valid-token":
            raise TokenValidationError
        return AccountContext(account_id=ACCOUNT_ID, tenant_id=TENANT_ID, scopes=self._scopes)


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
) -> tuple[TestClient, MemoryRepository]:
    storage = repository or MemoryRepository()
    use_case = CaptureManualOpportunity(storage, FixedClock(), FixedIds())
    app = create_http_app(
        StartService(CompleteMigrations()),
        capture_manual=use_case,
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
    with pytest.raises(ValidationError):
        ManualOpportunityDraft.model_validate({**draft(), "rawSource": "\n"})


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
            FixedIds(),
        )
        account = AccountContext(
            account_id=ACCOUNT_ID,
            tenant_id=TENANT_ID,
            scopes=("jobs:write",),
        )
        manual_draft = ManualOpportunityDraft.model_validate(draft())

        created = use_case.execute(account, manual_draft, "postgres-capture-0001")
        replayed = use_case.execute(account, manual_draft, "postgres-capture-0001")

        assert created.created
        assert not replayed.created
        assert replayed.opportunity == created.opportunity
        assert replayed.opportunity.source.raw_text == draft()["rawSource"]
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
