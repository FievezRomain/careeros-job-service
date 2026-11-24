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

    @field_validator("raw_source")
    @classmethod
    def require_source_wording(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("rawSource must contain source wording")
        return value


class SourceProvenance(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, populate_by_name=True)

    kind: Literal["manual"] = "manual"
    reference: SourceReference
    raw_text: str = Field(alias="rawText", min_length=1, max_length=100_000)
    captured_at: datetime = Field(alias="capturedAt")


class Opportunity(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, populate_by_name=True)

    opportunity_id: UUID = Field(alias="opportunityId")
    title: Title
    employer: Employer
    description: Description
    source: SourceProvenance
    created_at: datetime = Field(alias="createdAt")
