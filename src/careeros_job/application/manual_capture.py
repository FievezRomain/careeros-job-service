import hashlib
import json
from datetime import datetime
from typing import Literal, Protocol
from uuid import NAMESPACE_URL, UUID, uuid5

from pydantic import BaseModel, ConfigDict, Field

from careeros_job.domain.opportunity import (
    ManualOpportunityDraft,
    Opportunity,
    OpportunityOwner,
    normalize_display_text,
    normalize_identity_text,
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

    outcome: Literal["created", "replayed", "deduplicated", "conflict"]
    opportunity: Opportunity | None = None


class OpportunityRepository(Protocol):
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
    ) -> CaptureResult: ...

    def get(self, owner: OpportunityOwner, opportunity_id: UUID) -> Opportunity | None: ...

    def list_current(
        self, owner: OpportunityOwner, *, offset: int, limit: int
    ) -> tuple[tuple[Opportunity, ...], int]: ...


class ManualCaptureResult(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    opportunity: Opportunity
    created: bool


class PermissionDenied(Exception):
    pass


class IdempotencyKeyConflict(Exception):
    pass


class OpportunityNotFound(Exception):
    pass


class OpportunityPage(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, populate_by_name=True)

    items: tuple[Opportunity, ...]
    page: int = Field(ge=1)
    page_size: int = Field(alias="pageSize", ge=1, le=100)
    total: int = Field(ge=0)


def _digest(value: str) -> str:
    return hashlib.sha256(value.encode()).hexdigest()


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
            draft.model_dump(mode="json", by_alias=True, exclude_defaults=True),
            sort_keys=True,
            separators=(",", ":"),
        )
        normalized = draft.normalized()
        opportunity_id = self._identifiers.new()
        owner = OpportunityOwner(account_id=account.account_id, tenant_id=account.tenant_id)
        automatic_grouping_key = _digest(
            "\x1f".join(
                (
                    normalize_identity_text(normalized.title),
                    normalize_identity_text(normalized.employer),
                )
            )
        )
        grouping_key = (
            _digest(f"{automatic_grouping_key}\x1fseparate\x1f{opportunity_id}")
            if draft.force_separate
            else automatic_grouping_key
        )
        group_id = (
            opportunity_id
            if draft.force_separate
            else uuid5(
                NAMESPACE_URL,
                f"careeros-job-group:{owner.tenant_id}:{owner.account_id}:{grouping_key}",
            )
        )
        content_fingerprint = _digest(
            "\x1f".join(
                (
                    normalize_identity_text(normalized.title),
                    normalize_identity_text(normalized.employer),
                    normalize_identity_text(normalized.description),
                    normalize_display_text(normalized.raw_source),
                )
            )
        )
        result = self._repository.capture(
            owner,
            normalized,
            opportunity_id=opportunity_id,
            group_id=group_id,
            captured_at=self._clock.now(),
            idempotency_key=idempotency_key,
            request_fingerprint=_digest(serialized),
            grouping_key=grouping_key,
            content_fingerprint=content_fingerprint,
        )
        if result.outcome == "conflict":
            raise IdempotencyKeyConflict
        if result.opportunity is None:
            raise RuntimeError("capture outcome is missing its opportunity")
        return ManualCaptureResult(
            opportunity=result.opportunity,
            created=result.outcome == "created",
        )


class QueryOpportunityCatalog:
    def __init__(self, repository: OpportunityRepository) -> None:
        self._repository = repository

    @staticmethod
    def _owner(account: AccountContext) -> OpportunityOwner:
        if "jobs:read" not in account.scopes:
            raise PermissionDenied
        return OpportunityOwner(account_id=account.account_id, tenant_id=account.tenant_id)

    def list(self, account: AccountContext, *, page: int, page_size: int) -> OpportunityPage:
        items, total = self._repository.list_current(
            self._owner(account), offset=(page - 1) * page_size, limit=page_size
        )
        return OpportunityPage(items=items, page=page, page_size=page_size, total=total)

    def get(self, account: AccountContext, opportunity_id: UUID) -> Opportunity:
        opportunity = self._repository.get(self._owner(account), opportunity_id)
        if opportunity is None:
            raise OpportunityNotFound
        return opportunity
