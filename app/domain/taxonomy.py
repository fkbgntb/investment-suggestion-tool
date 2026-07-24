"""Versioned data-only taxonomy and information source contracts."""

from __future__ import annotations

from urllib.parse import urlsplit

from pydantic import AnyHttpUrl, AwareDatetime, Field, field_validator, model_validator

from app.domain.base import DomainModel, Identifier, UnitInterval
from app.domain.enums import (
    EntityType,
    ExposureDataStatus,
    ExposureKind,
    InfluenceDirection,
    SourceKind,
    SourceRole,
    TaxonomyNodeKind,
    TopicCategory,
    TrustTier,
)


def _validate_plain_text(value: str) -> str:
    if any(ord(character) < 32 and character not in {"\t", "\n"} for character in value):
        raise ValueError("taxonomy text cannot contain control characters")
    return value


class Topic(DomainModel):
    topic_id: Identifier
    name: str = Field(min_length=1, max_length=120)
    category: TopicCategory = TopicCategory.THEME
    parent_topic_id: Identifier | None = None
    aliases: tuple[str, ...] = Field(default_factory=tuple, max_length=100)
    keywords: tuple[str, ...] = Field(default_factory=tuple, max_length=500)
    enabled: bool = True
    config_version: str = Field(min_length=1, max_length=64)

    _plain_name = field_validator("name")(_validate_plain_text)

    @field_validator("aliases", "keywords")
    @classmethod
    def validate_search_terms(cls, values: tuple[str, ...]) -> tuple[str, ...]:
        cleaned = tuple(_validate_plain_text(value.strip()) for value in values)
        if any(not value for value in cleaned):
            raise ValueError("taxonomy aliases and keywords cannot be blank")
        if len({value.casefold() for value in cleaned}) != len(cleaned):
            raise ValueError("taxonomy aliases and keywords must be unique")
        return cleaned

    @model_validator(mode="after")
    def validate_hierarchy(self) -> Topic:
        if self.category is TopicCategory.THEME and self.parent_topic_id is not None:
            raise ValueError("root themes cannot have a parent topic")
        if self.category is not TopicCategory.THEME and self.parent_topic_id is None:
            raise ValueError("subthemes and end markets require a parent topic")
        if self.parent_topic_id == self.topic_id:
            raise ValueError("a topic cannot be its own parent")
        return self


class Entity(DomainModel):
    entity_id: Identifier
    name: str = Field(min_length=1, max_length=160)
    entity_type: EntityType
    aliases: tuple[str, ...] = Field(default_factory=tuple, max_length=100)
    enabled: bool = True
    config_version: str = Field(default="unversioned", min_length=1, max_length=64)

    _plain_name = field_validator("name")(_validate_plain_text)

    @field_validator("aliases")
    @classmethod
    def validate_aliases(cls, values: tuple[str, ...]) -> tuple[str, ...]:
        cleaned = tuple(_validate_plain_text(value.strip()) for value in values)
        if any(not value for value in cleaned):
            raise ValueError("entity aliases cannot be blank")
        if len({value.casefold() for value in cleaned}) != len(cleaned):
            raise ValueError("entity aliases must be unique")
        return cleaned


class InfluenceRelation(DomainModel):
    relation_id: Identifier
    source_kind: TaxonomyNodeKind
    source_id: Identifier
    target_kind: TaxonomyNodeKind
    target_id: Identifier
    kind: ExposureKind
    direction: InfluenceDirection
    rationale: str = Field(min_length=1, max_length=1000)
    enabled: bool = True
    config_version: str = Field(min_length=1, max_length=64)

    _plain_rationale = field_validator("rationale")(_validate_plain_text)

    @model_validator(mode="after")
    def reject_self_reference(self) -> InfluenceRelation:
        if self.source_kind is self.target_kind and self.source_id == self.target_id:
            raise ValueError("an influence relation cannot point to itself")
        return self


class Exposure(DomainModel):
    exposure_id: Identifier
    asset_id: Identifier
    topic_id: Identifier
    entity_id: Identifier | None = None
    kind: ExposureKind
    weight: UnitInterval | None = None
    data_status: ExposureDataStatus = ExposureDataStatus.UNKNOWN
    rationale: str = Field(min_length=1, max_length=1000)
    enabled: bool = True
    config_version: str = Field(min_length=1, max_length=64)

    _plain_rationale = field_validator("rationale")(_validate_plain_text)

    @model_validator(mode="before")
    @classmethod
    def infer_legacy_weight_provenance(cls, value: object) -> object:
        if (
            isinstance(value, dict)
            and "data_status" not in value
            and value.get("weight") is not None
        ):
            return {**value, "data_status": ExposureDataStatus.HEURISTIC}
        return value

    @model_validator(mode="after")
    def validate_weight_provenance(self) -> Exposure:
        if self.data_status is ExposureDataStatus.UNKNOWN and self.weight is not None:
            raise ValueError("unknown exposure data cannot include a weight")
        if self.data_status is not ExposureDataStatus.UNKNOWN and self.weight is None:
            raise ValueError("known exposure data requires a weight")
        return self


class TaxonomyConfiguration(DomainModel):
    """An immutable, complete configuration version published by a trusted local actor."""

    configuration_id: Identifier
    config_version: str = Field(min_length=1, max_length=64)
    name: str = Field(min_length=1, max_length=160)
    topics: tuple[Topic, ...] = Field(min_length=1, max_length=500)
    entities: tuple[Entity, ...] = Field(default_factory=tuple, max_length=2000)
    influence_relations: tuple[InfluenceRelation, ...] = Field(
        default_factory=tuple, max_length=5000
    )
    exposures: tuple[Exposure, ...] = Field(default_factory=tuple, max_length=5000)
    created_at: AwareDatetime
    based_on_version: str | None = Field(default=None, max_length=64)

    _plain_name = field_validator("name")(_validate_plain_text)

    @model_validator(mode="after")
    def validate_configuration_graph(self) -> TaxonomyConfiguration:
        collections = {
            "topic": tuple(topic.topic_id for topic in self.topics),
            "entity": tuple(entity.entity_id for entity in self.entities),
            "relation": tuple(relation.relation_id for relation in self.influence_relations),
            "exposure": tuple(exposure.exposure_id for exposure in self.exposures),
        }
        for label, identifiers in collections.items():
            if len(set(identifiers)) != len(identifiers):
                raise ValueError(f"duplicate {label} IDs are not allowed")

        versioned = (*self.topics, *self.entities, *self.influence_relations, *self.exposures)
        if any(item.config_version != self.config_version for item in versioned):
            raise ValueError("every taxonomy item must use the configuration version")

        topics = {topic.topic_id: topic for topic in self.topics}
        entities = {entity.entity_id for entity in self.entities}
        for topic in self.topics:
            if topic.parent_topic_id is not None and topic.parent_topic_id not in topics:
                raise ValueError("topic parent must exist in the same configuration")
        self._validate_topic_cycles(topics)

        for relation in self.influence_relations:
            self._assert_node_exists(relation.source_kind, relation.source_id, topics, entities)
            self._assert_node_exists(relation.target_kind, relation.target_id, topics, entities)

        for exposure in self.exposures:
            if exposure.topic_id not in topics:
                raise ValueError("exposure topic must exist in the same configuration")
            if exposure.entity_id is not None and exposure.entity_id not in entities:
                raise ValueError("exposure entity must exist in the same configuration")
        return self

    @staticmethod
    def _assert_node_exists(
        kind: TaxonomyNodeKind,
        identifier: str,
        topics: dict[str, Topic],
        entities: set[str],
    ) -> None:
        exists = identifier in topics if kind is TaxonomyNodeKind.TOPIC else identifier in entities
        if not exists:
            raise ValueError("influence relation node must exist in the same configuration")

    @staticmethod
    def _validate_topic_cycles(topics: dict[str, Topic]) -> None:
        for start_id in topics:
            seen: set[str] = set()
            current_id: str | None = start_id
            while current_id is not None:
                if current_id in seen:
                    raise ValueError("topic hierarchy cannot contain a cycle")
                seen.add(current_id)
                current_id = topics[current_id].parent_topic_id


class Source(DomainModel):
    source_id: Identifier
    name: str = Field(min_length=1, max_length=160)
    kind: SourceKind
    role: SourceRole | None = None
    trust_tier: TrustTier
    base_url: AnyHttpUrl
    regions: tuple[str, ...] = Field(min_length=1, max_length=50)
    languages: tuple[str, ...] = Field(min_length=1, max_length=20)
    enabled: bool = True
    adapter_name: Identifier
    crawl_interval_hours: int = Field(default=3, ge=1, le=168)
    allow_fulltext: bool = False
    allowed_domains: tuple[str, ...] = Field(default_factory=tuple, min_length=1, max_length=100)
    terms_url: AnyHttpUrl | None = None
    config_version: str = Field(default="1.0", min_length=1, max_length=64)

    @model_validator(mode="before")
    @classmethod
    def infer_legacy_allowed_domain(cls, value: object) -> object:
        if isinstance(value, dict) and "allowed_domains" not in value:
            hostname = urlsplit(str(value.get("base_url", ""))).hostname
            if hostname:
                return {**value, "allowed_domains": (hostname,)}
        return value

    @field_validator("regions", "languages")
    @classmethod
    def normalize_codes(cls, values: tuple[str, ...]) -> tuple[str, ...]:
        normalized = tuple(value.strip().casefold() for value in values)
        if any(not value or len(value) > 32 for value in normalized):
            raise ValueError("source region and language codes must be short non-empty values")
        if len(set(normalized)) != len(normalized):
            raise ValueError("source region and language codes must be unique")
        return normalized

    @field_validator("allowed_domains")
    @classmethod
    def normalize_allowed_domains(cls, values: tuple[str, ...]) -> tuple[str, ...]:
        normalized: list[str] = []
        for value in values:
            domain = value.strip().rstrip(".").encode("idna").decode("ascii").casefold()
            if not domain or "*" in domain or "/" in domain or ":" in domain:
                raise ValueError("allowed domains must be explicit hostnames")
            normalized.append(domain)
        if len(set(normalized)) != len(normalized):
            raise ValueError("allowed domains must be unique")
        return tuple(normalized)

    @model_validator(mode="after")
    def community_sources_are_sentiment_only(self) -> Source:
        if self.kind in {SourceKind.COMMUNITY, SourceKind.SOCIAL} and (
            self.trust_tier is not TrustTier.SENTIMENT_ONLY
        ):
            raise ValueError("community and social sources can only be used as sentiment")
        urls = (self.base_url, *((self.terms_url,) if self.terms_url is not None else ()))
        for url in urls:
            if url.scheme != "https":
                raise ValueError("source configuration URLs must use HTTPS")
            if url.username is not None or url.password is not None:
                raise ValueError("source configuration URLs cannot contain credentials")
        base_host = (self.base_url.host or "").encode("idna").decode("ascii").casefold()
        if base_host not in self.allowed_domains:
            raise ValueError("the base URL host must be explicitly allowed")
        if self.role is None:
            inferred = {
                SourceKind.OFFICIAL: SourceRole.OFFICIAL_DISCLOSURE,
                SourceKind.REGULATOR: SourceRole.OFFICIAL_DISCLOSURE,
                SourceKind.FUND_MANAGER: SourceRole.OFFICIAL_DISCLOSURE,
                SourceKind.COMPANY_OFFICIAL: SourceRole.OFFICIAL_DISCLOSURE,
                SourceKind.MARKET_DATA: SourceRole.MARKET_DATA,
                SourceKind.RESEARCH: SourceRole.INDUSTRY_DATA,
                SourceKind.SOCIAL: SourceRole.SENTIMENT,
                SourceKind.COMMUNITY: SourceRole.SENTIMENT,
            }.get(self.kind, SourceRole.NEWS_DISCOVERY)
            object.__setattr__(self, "role", inferred)
        return self
