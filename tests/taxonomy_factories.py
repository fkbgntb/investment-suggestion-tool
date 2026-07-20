from __future__ import annotations

from datetime import UTC, datetime

from app.domain.enums import (
    EntityType,
    ExposureKind,
    InfluenceDirection,
    TaxonomyNodeKind,
    TopicCategory,
)
from app.domain.taxonomy import (
    Entity,
    Exposure,
    InfluenceRelation,
    TaxonomyConfiguration,
    Topic,
)


def taxonomy_configuration(
    version: str = "test-1.0.0",
    *,
    based_on_version: str | None = None,
    topic_enabled: bool = True,
    asset_id: str = "asset-007300",
) -> TaxonomyConfiguration:
    root = Topic(
        topic_id="test-semiconductor",
        name="Test Semiconductor",
        category=TopicCategory.THEME,
        aliases=("测试半导体",),
        keywords=("test chip",),
        enabled=topic_enabled,
        config_version=version,
    )
    copper = Entity(
        entity_id="test-copper",
        name="Test Copper",
        entity_type=EntityType.COMMODITY,
        aliases=("测试铜",),
        config_version=version,
    )
    relation = InfluenceRelation(
        relation_id="test-copper-input-cost",
        source_kind=TaxonomyNodeKind.ENTITY,
        source_id=copper.entity_id,
        target_kind=TaxonomyNodeKind.TOPIC,
        target_id=root.topic_id,
        kind=ExposureKind.INPUT_COST,
        direction=InfluenceDirection.CONTEXT_DEPENDENT,
        rationale="Stored as data and interpreted by later deterministic rules.",
        config_version=version,
    )
    exposure = Exposure(
        exposure_id=f"test-exposure-{version}",
        asset_id=asset_id,
        topic_id=root.topic_id,
        kind=ExposureKind.DIRECT,
        rationale="Test topic mapping without an invented quantitative weight.",
        config_version=version,
    )
    return TaxonomyConfiguration(
        configuration_id=f"taxonomy-{version}",
        config_version=version,
        name=f"Test taxonomy {version}",
        topics=(root,),
        entities=(copper,),
        influence_relations=(relation,),
        exposures=(exposure,),
        created_at=datetime(2026, 7, 19, 0, 0, tzinfo=UTC),
        based_on_version=based_on_version,
    )
