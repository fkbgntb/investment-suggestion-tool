import json
from pathlib import Path

from app.domain.schema import build_domain_schema, render_domain_schema


def test_committed_schema_matches_domain_models() -> None:
    root = Path(__file__).resolve().parents[1]
    committed = (root / "schemas" / "domain-contracts-v1.json").read_text(encoding="utf-8")
    assert committed == render_domain_schema()


def test_schema_is_versioned_and_rejects_unknown_properties() -> None:
    schema = build_domain_schema()
    assert schema["$schema"] == "https://json-schema.org/draft/2020-12/schema"
    assert schema["x-schema-version"] == "1.0"
    assert schema["additionalProperties"] is False

    serialized = json.dumps(schema, ensure_ascii=False)
    for value in (
        "DISCOVERED",
        "PUBLISHED",
        "RETRYABLE_FAILED",
        "INSUFFICIENT_DATA",
        "SMALL_ADD",
    ):
        assert value in serialized


def test_raw_document_schema_keeps_external_and_control_sections_separate() -> None:
    definitions = build_domain_schema()["$defs"]
    raw_properties = definitions["RawDocument"]["properties"]
    assert set(raw_properties) == {"schema_version", "external", "control"}
    assert "body" not in raw_properties
    assert "state" not in raw_properties
