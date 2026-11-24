import re
import unicodedata
from datetime import datetime
from typing import Annotated, Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, StringConstraints, field_validator

Title = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=200)]
Employer = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=200)]
Description = Annotated[
    str, StringConstraints(strip_whitespace=True, min_length=1, max_length=10_000)
]
SourceReference = Annotated[
    str, StringConstraints(strip_whitespace=True, min_length=1, max_length=2_000)
]
_WHITESPACE = re.compile(r"\s+")


def normalize_display_text(value: str) -> str:
    return _WHITESPACE.sub(" ", unicodedata.normalize("NFKC", value)).strip()


def normalize_identity_text(value: str) -> str:
    return normalize_display_text(value).casefold()


class OpportunityOwner(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    account_id: UUID
    tenant_id: UUID


class ManualOpportunityDraft(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, populate_by_name=True)

    title: Title
    employer: Employer
    description: Description
    source_reference: SourceReference = Field(alias="sourceReference")
    raw_source: str = Field(alias="rawSource", min_length=1, max_length=100_000)
    force_separate: bool = Field(default=False, alias="forceSeparate")

    @field_validator("raw_source")
    @classmethod
    def require_source_wording(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("rawSource must contain source wording")
        return value

    def normalized(self) -> ManualOpportunityDraft:
        return self.model_copy(
            update={
                "title": normalize_display_text(self.title),
                "employer": normalize_display_text(self.employer),
                "description": normalize_display_text(self.description),
            }
        )


class SourceProvenance(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, populate_by_name=True)

    kind: Literal["manual"] = "manual"
    reference: SourceReference
    raw_text: str = Field(alias="rawText", min_length=1, max_length=100_000)
    captured_at: datetime = Field(alias="capturedAt")


class OpportunityEvidence(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, populate_by_name=True)

    stated_fields: tuple[str, ...] = Field(
        default=("title", "employer", "description"), alias="statedFields"
    )
    inferred_fields: tuple[str, ...] = Field(default=(), alias="inferredFields")
    unknown_fields: tuple[str, ...] = Field(
        default=("location", "contractType", "workMode"), alias="unknownFields"
    )


class Opportunity(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, populate_by_name=True)

    opportunity_id: UUID = Field(alias="opportunityId")
    group_id: UUID = Field(alias="groupId")
    version: int = Field(ge=1)
    title: Title
    employer: Employer
    description: Description
    source: SourceProvenance
    evidence: OpportunityEvidence = Field(default_factory=OpportunityEvidence)
    created_at: datetime = Field(alias="createdAt")
