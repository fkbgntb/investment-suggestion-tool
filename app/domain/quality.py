"""Versioned quality metrics and advisory-only shadow-run records."""

from __future__ import annotations

from decimal import Decimal
from typing import Literal

from pydantic import AwareDatetime, Field

from app.domain.base import DomainModel, Identifier, UnitInterval
from app.domain.enums import SuggestionLabel


class QualityMetrics(DomainModel):
    metric_version: Literal["quality-metrics-1.0.0"] = "quality-metrics-1.0.0"
    evaluated_at: AwareDatetime
    raw_document_count: int = Field(ge=0)
    relevance_assessment_count: int = Field(ge=0)
    duplicate_document_count: int = Field(ge=0)
    crawl_run_count: int = Field(ge=0)
    ai_extraction_run_count: int = Field(ge=0)
    irrelevant_ratio: UnitInterval
    duplicate_ratio: UnitInterval
    source_failure_ratio: UnitInterval
    ai_schema_failure_ratio: UnitInterval
    analysis_input_tokens: int = Field(ge=0)
    analysis_output_tokens: int = Field(ge=0)
    estimated_cost_cny: Decimal | None = Field(default=None, ge=0)


class ShadowRunRecord(DomainModel):
    record_version: Literal["shadow-run-1.0.0"] = "shadow-run-1.0.0"
    shadow_run_id: Identifier
    started_at: AwareDatetime
    finished_at: AwareDatetime
    position_id: Identifier
    report_id: Identifier
    previous_report_id: Identifier | None = None
    decision_label: SuggestionLabel
    decision_changed: bool
    change_reasons: tuple[str, ...] = Field(min_length=1, max_length=20)
    evidence_count: int = Field(ge=0)
    failed_source_count: int = Field(ge=0)
    provider_name: Identifier
    model_version: str = Field(min_length=1, max_length=120)
    prompt_version: str = Field(min_length=1, max_length=120)
    rule_version: str = Field(min_length=1, max_length=120)
    pipeline_version: str = Field(min_length=1, max_length=120)
    metrics: QualityMetrics
    status: Literal["SUCCEEDED", "DEGRADED"]
    advisory_only: Literal[True] = True
