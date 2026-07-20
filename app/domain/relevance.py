"""Explainable, versioned document relevance decisions and user labels."""

from __future__ import annotations

from pydantic import AwareDatetime, Field

from app.domain.base import DomainModel, Identifier, UnitInterval
from app.domain.enums import RelevanceLabel


class RelevanceRuleHit(DomainModel):
    rule_type: str = Field(min_length=1, max_length=64)
    term: str = Field(min_length=1, max_length=160)
    target_id: Identifier
    points: UnitInterval


class RelevanceAssessment(DomainModel):
    assessment_id: Identifier
    document_id: Identifier
    label: RelevanceLabel
    score: UnitInterval
    topic_ids: tuple[Identifier, ...] = Field(default_factory=tuple, max_length=50)
    entity_ids: tuple[Identifier, ...] = Field(default_factory=tuple, max_length=100)
    hits: tuple[RelevanceRuleHit, ...] = Field(default_factory=tuple, max_length=500)
    reasons: tuple[str, ...] = Field(min_length=1, max_length=50)
    rule_version: str = Field(min_length=1, max_length=120)
    taxonomy_version: str = Field(min_length=1, max_length=64)
    assessed_at: AwareDatetime


class HumanRelevanceLabel(DomainModel):
    label_id: Identifier
    document_id: Identifier
    label: RelevanceLabel
    note: str | None = Field(default=None, max_length=1000)
    labeled_at: AwareDatetime
    actor: str = Field(default="local_user", min_length=1, max_length=64)
