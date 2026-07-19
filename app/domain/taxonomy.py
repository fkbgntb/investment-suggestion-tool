"""Configurable topics, entities, exposures, and information sources."""

from __future__ import annotations

from pydantic import AnyHttpUrl, Field, model_validator

from app.domain.base import DomainModel, Identifier, UnitInterval
from app.domain.enums import EntityType, ExposureKind, SourceKind, TrustTier


class Topic(DomainModel):
    topic_id: Identifier
    name: str = Field(min_length=1, max_length=120)
    aliases: tuple[str, ...] = Field(default_factory=tuple, max_length=100)
    keywords: tuple[str, ...] = Field(default_factory=tuple, max_length=500)
    enabled: bool = True
    config_version: str = Field(min_length=1, max_length=64)


class Entity(DomainModel):
    entity_id: Identifier
    name: str = Field(min_length=1, max_length=160)
    entity_type: EntityType
    aliases: tuple[str, ...] = Field(default_factory=tuple, max_length=100)


class Exposure(DomainModel):
    exposure_id: Identifier
    asset_id: Identifier
    topic_id: Identifier
    entity_id: Identifier | None = None
    kind: ExposureKind
    weight: UnitInterval
    rationale: str = Field(min_length=1, max_length=1000)
    config_version: str = Field(min_length=1, max_length=64)


class Source(DomainModel):
    source_id: Identifier
    name: str = Field(min_length=1, max_length=160)
    kind: SourceKind
    trust_tier: TrustTier
    base_url: AnyHttpUrl
    regions: tuple[str, ...] = Field(min_length=1, max_length=50)
    languages: tuple[str, ...] = Field(min_length=1, max_length=20)
    enabled: bool = True
    adapter_name: Identifier

    @model_validator(mode="after")
    def community_sources_are_sentiment_only(self) -> Source:
        if self.kind is SourceKind.COMMUNITY and self.trust_tier is not TrustTier.SENTIMENT_ONLY:
            raise ValueError("community sources can only be used as sentiment")
        return self
