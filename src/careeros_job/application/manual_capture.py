import hashlib
import json
from datetime import datetime
from typing import Literal, Protocol
from uuid import UUID

from pydantic import BaseModel, ConfigDict

from careeros_job.domain.opportunity import (
    ManualOpportunityDraft,
    Opportunity,
    OpportunityOwner,
)


class AccountContext(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    account_id: UUID
    tenant_id: UUID
    scopes: tuple[str, ...] = ()


class TokenValidationError(Exception):
    pass


class TokenVerifier(Protocol):
    def verify(self, token: str) -> AccountContext: ...


class Clock(Protocol):
    def now(self) -> datetime: ...


class IdentifierFactory(Protocol):
    def new(self) -> UUID: ...


class CaptureResult(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    outcome: Literal["created", "replayed", "conflict"]
    opportunity: Opportunity | None = None


class OpportunityRepository(Protocol):
    def capture(
        self,
        owner: OpportunityOwner,
        draft: ManualOpportunityDraft,
        *,
        opportunity_id: UUID,
        captured_at: datetime,
        idempotency_key: str,
        request_fingerprint: str,
    ) -> CaptureResult: ...


class ManualCaptureResult(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    opportunity: Opportunity
    created: bool


class PermissionDenied(Exception):
    pass


class IdempotencyKeyConflict(Exception):
    pass


class CaptureManualOpportunity:
    def __init__(
        self,
        repository: OpportunityRepository,
        clock: Clock,
        identifiers: IdentifierFactory,
    ) -> None:
        self._repository = repository
        self._clock = clock
        self._identifiers = identifiers

    def execute(
        self,
        account: AccountContext,
        draft: ManualOpportunityDraft,
        idempotency_key: str,
    ) -> ManualCaptureResult:
        if "jobs:write" not in account.scopes:
            raise PermissionDenied
        serialized = json.dumps(
            draft.model_dump(mode="json", by_alias=True),
            sort_keys=True,
            separators=(",", ":"),
        )
        result = self._repository.capture(
            OpportunityOwner(account_id=account.account_id, tenant_id=account.tenant_id),
            draft,
            opportunity_id=self._identifiers.new(),
            captured_at=self._clock.now(),
            idempotency_key=idempotency_key,
            request_fingerprint=hashlib.sha256(serialized.encode()).hexdigest(),
        )
        if result.outcome == "conflict":
            raise IdempotencyKeyConflict
        if result.opportunity is None:
            raise RuntimeError("capture outcome is missing its opportunity")
        return ManualCaptureResult(
            opportunity=result.opportunity,
            created=result.outcome == "created",
        )
