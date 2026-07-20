from __future__ import annotations

import ast
from pathlib import Path

import pytest
from pydantic import ValidationError

from app.domain.enums import ExposureDataStatus, TopicCategory
from app.domain.taxonomy import Exposure, TaxonomyConfiguration, Topic
from scripts.seed_demo_taxonomy import load_demo_configuration
from tests.taxonomy_factories import taxonomy_configuration


def test_semiconductor_configuration_covers_required_scope_without_fake_holdings() -> None:
    configuration = load_demo_configuration()

    assert len(configuration.topics) == 10
    assert len(configuration.entities) == 16
    assert len(configuration.influence_relations) == 23
    assert len(configuration.exposures) == 8
    assert {topic.name for topic in configuration.topics} >= {
        "半导体",
        "存储",
        "晶圆",
        "半导体设备",
        "芯片设计",
        "晶圆代工",
        "手机终端",
        "PC终端",
        "服务器终端",
        "汽车终端",
    }
    assert {entity.name for entity in configuration.entities} >= {"铜", "硅", "Micron Technology"}
    assert all(exposure.weight is None for exposure in configuration.exposures)
    assert all(
        exposure.data_status is ExposureDataStatus.UNKNOWN for exposure in configuration.exposures
    )
    company_ids = {
        entity.entity_id
        for entity in configuration.entities
        if entity.entity_type.value == "COMPANY"
    }
    assert not any(exposure.entity_id in company_ids for exposure in configuration.exposures)


def test_new_topic_and_enabled_state_are_pure_configuration_data() -> None:
    original = taxonomy_configuration()
    payload = original.model_dump(mode="json")
    payload["topics"].append(
        Topic(
            topic_id="test-semiconductor.gold",
            name="Test Gold",
            category=TopicCategory.SUBTHEME,
            parent_topic_id="test-semiconductor",
            enabled=False,
            config_version=original.config_version,
        ).model_dump(mode="json")
    )

    updated = TaxonomyConfiguration.model_validate(payload)
    assert updated.topics[-1].enabled is False
    assert updated.topics[-1].topic_id == "test-semiconductor.gold"


def test_configuration_rejects_unknown_nodes_cycles_and_executable_fields() -> None:
    payload = taxonomy_configuration().model_dump(mode="json")
    payload["influence_relations"][0]["target_id"] = "missing-topic"
    with pytest.raises(ValidationError, match="node must exist"):
        TaxonomyConfiguration.model_validate(payload)

    payload = taxonomy_configuration().model_dump(mode="json")
    payload["topics"][0]["category"] = "SUBTHEME"
    payload["topics"][0]["parent_topic_id"] = "test-semiconductor"
    with pytest.raises(ValidationError, match="own parent|cycle"):
        TaxonomyConfiguration.model_validate(payload)

    payload = taxonomy_configuration().model_dump(mode="json")
    payload["python_expression"] = "__import__('os').system('unsafe')"
    with pytest.raises(ValidationError, match="Extra inputs"):
        TaxonomyConfiguration.model_validate(payload)


def test_exposure_requires_explicit_weight_provenance() -> None:
    base = taxonomy_configuration().exposures[0].model_dump()
    base["weight"] = "0.5"
    with pytest.raises(ValidationError, match="unknown exposure"):
        Exposure.model_validate(base)

    base["data_status"] = ExposureDataStatus.HEURISTIC
    assert Exposure.model_validate(base).weight is not None


def test_taxonomy_publication_never_executes_configuration_text() -> None:
    root = Path(__file__).resolve().parents[1]
    forbidden_calls = {"compile", "eval", "exec"}
    for relative_path in (
        "app/domain/taxonomy.py",
        "app/services/taxonomy.py",
        "scripts/seed_demo_taxonomy.py",
    ):
        tree = ast.parse((root / relative_path).read_text(encoding="utf-8"))
        calls = {
            node.func.id
            for node in ast.walk(tree)
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
        }
        assert calls.isdisjoint(forbidden_calls)
