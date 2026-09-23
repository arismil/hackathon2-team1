"""Evidence references and source provenance metadata only."""

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field


class SourceReference(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    source_id: str = Field(min_length=1)
    locator: str | None = Field(default=None, min_length=1)
    version: str | None = Field(default=None, min_length=1)


class EvidenceReference(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    evidence_id: str = Field(min_length=1)
    source: SourceReference
    retrieved_at: AwareDatetime
