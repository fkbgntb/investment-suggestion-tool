"""Evidence extraction outputs and deterministic scoring inputs."""

from __future__ import annotations

from typing import Literal

from pydantic import AwareDatetime, Field, model_validator

from app.domain.base import DomainModel, Identifier, UnitInterval
from app.domain.enums import EvidenceDirection, SourceKind, TrustTier


class EvidenceDraft(DomainModel):
    """AI-produced claim data with no persistence or control instructions."""

    claim: str = Field(min_length=1, max_length=4000)
    direction: EvidenceDirection
    quote: str | None = Field(default=None, max_length=2000)
    topic_ids: tuple[Identifier, ...] = Field(min_length=1, max_length=50)
    entity_ids: tuple[Identifier, ...] = Field(default_factory=tuple, max_length=100)
    confidence: UnitInterval
    uncertainty: str | None = Field(default=None, max_length=2000)
    claim_type: str = Field(min_length=1, max_length=120)
    impact_horizon: Literal["SHORT", "MEDIUM", "LONG", "UNKNOWN"]
    directness: UnitInterval


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
    directness: UnitInterval = 1
    extraction_confidence: UnitInterval
    total: UnitInterval
    source_kind: SourceKind = SourceKind.NEWS
    trust_tier: TrustTier = TrustTier.SECONDARY
    independent_source_count: int = Field(default=1, ge=1, le=1000)
    same_origin_reprint: bool = False
    confidence_cap: UnitInterval | None = None
    component_reasons: tuple[str, ...] = Field(default_factory=tuple, max_length=20)
    scoring_version: str = Field(min_length=1, max_length=120)
    scored_at: AwareDatetime

    @model_validator(mode="after")
    def total_cannot_exceed_weakest_dimension(self) -> EvidenceScore:
        dimensions = (
            self.source_quality,
            self.independence,
            self.recency,
            self.relevance,
            self.directness,
            self.extraction_confidence,
        )
        if self.total > min(dimensions):
            raise ValueError("multiplicative total cannot exceed its weakest score dimension")
        return self


class EvidenceExtractionRequest(DomainModel):
    """Minimal AI input: normalized content, never database control or portfolio details."""

    document_id: Identifier
    title: str = Field(min_length=1, max_length=1000)
    summary: str | None = Field(default=None, max_length=20_000)
    normalized_body: str = Field(min_length=1, max_length=100_000)
    language: str = Field(min_length=2, max_length=16, pattern=r"^[A-Za-z-]+$")
    topic_ids: tuple[Identifier, ...] = Field(min_length=1, max_length=50)
    entity_ids: tuple[Identifier, ...] = Field(default_factory=tuple, max_length=100)
    source_kind: str = Field(default="UNKNOWN", min_length=1, max_length=64)
    published_at: AwareDatetime | None = None
    suspicious_flags: tuple[str, ...] = Field(default_factory=tuple, max_length=50)
    prompt_version: str = Field(min_length=1, max_length=120)


class EvidenceModelOutput(DomainModel):
    """Strict JSON object the model may produce; provenance remains local control data."""

    document_id: Identifier
    relevance: UnitInterval
    event_type: str = Field(min_length=1, max_length=160)
    related_topics: tuple[Identifier, ...] = Field(default_factory=tuple, max_length=50)
    related_entities: tuple[Identifier, ...] = Field(default_factory=tuple, max_length=100)
    claims: tuple[EvidenceDraft, ...] = Field(default_factory=tuple, max_length=50)
    uncertainties: tuple[str, ...] = Field(default_factory=tuple, max_length=50)


class EvidenceExtractionResult(DomainModel):
    document_id: Identifier
    evidence: tuple[EvidenceDraft, ...] = Field(default_factory=tuple, max_length=200)
    unknowns: tuple[str, ...] = Field(default_factory=tuple, max_length=100)
    provider_name: Identifier
    model_version: str = Field(min_length=1, max_length=120)
    prompt_version: str = Field(min_length=1, max_length=120)
    completed_at: AwareDatetime
    relevance: UnitInterval = 0
    event_type: str = Field(default="unknown", min_length=1, max_length=160)
    related_topic_ids: tuple[Identifier, ...] = Field(default_factory=tuple, max_length=50)
    related_entity_ids: tuple[Identifier, ...] = Field(default_factory=tuple, max_length=100)
    source_is_primary: bool = False
    input_tokens: int = Field(default=0, ge=0)
    output_tokens: int = Field(default=0, ge=0)
    elapsed_ms: int = Field(default=0, ge=0)
