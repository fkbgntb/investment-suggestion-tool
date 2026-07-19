"""Evidence extraction outputs and deterministic scoring inputs."""

from __future__ import annotations

from pydantic import AwareDatetime, Field, model_validator

from app.domain.base import DomainModel, Identifier, UnitInterval
from app.domain.enums import EvidenceDirection


class EvidenceDraft(DomainModel):
    """AI-produced claim data with no persistence or control instructions."""

    claim: str = Field(min_length=1, max_length=4000)
    direction: EvidenceDirection
    quote: str | None = Field(default=None, max_length=2000)
    topic_ids: tuple[Identifier, ...] = Field(min_length=1, max_length=50)
    entity_ids: tuple[Identifier, ...] = Field(default_factory=tuple, max_length=100)
    confidence: UnitInterval
    uncertainty: str | None = Field(default=None, max_length=2000)


class Evidence(DomainModel):
    evidence_id: Identifier
    document_id: Identifier
    cluster_id: Identifier | None = None
    draft: EvidenceDraft
    extracted_at: AwareDatetime
    extractor_name: Identifier
    model_version: str = Field(min_length=1, max_length=120)
    prompt_version: str = Field(min_length=1, max_length=120)


class EvidenceScore(DomainModel):
    evidence_id: Identifier
    source_quality: UnitInterval
    independence: UnitInterval
    recency: UnitInterval
    relevance: UnitInterval
    extraction_confidence: UnitInterval
    total: UnitInterval
    scoring_version: str = Field(min_length=1, max_length=120)
    scored_at: AwareDatetime

    @model_validator(mode="after")
    def total_cannot_exceed_strongest_dimension(self) -> EvidenceScore:
        dimensions = (
            self.source_quality,
            self.independence,
            self.recency,
            self.relevance,
            self.extraction_confidence,
        )
        if self.total > max(dimensions):
            raise ValueError("total score cannot exceed every score dimension")
        return self


class EvidenceExtractionRequest(DomainModel):
    """Minimal AI input: normalized content, never database control or portfolio details."""

    document_id: Identifier
    title: str = Field(min_length=1, max_length=1000)
    normalized_body: str = Field(min_length=1, max_length=200_000)
    language: str = Field(min_length=2, max_length=16, pattern=r"^[A-Za-z-]+$")
    topic_ids: tuple[Identifier, ...] = Field(min_length=1, max_length=50)
    prompt_version: str = Field(min_length=1, max_length=120)


class EvidenceExtractionResult(DomainModel):
    document_id: Identifier
    evidence: tuple[EvidenceDraft, ...] = Field(default_factory=tuple, max_length=200)
    unknowns: tuple[str, ...] = Field(default_factory=tuple, max_length=100)
    provider_name: Identifier
    model_version: str = Field(min_length=1, max_length=120)
    prompt_version: str = Field(min_length=1, max_length=120)
    completed_at: AwareDatetime
