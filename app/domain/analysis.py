"""Advisory-only decision, analysis, and report models."""

from __future__ import annotations

from typing import Literal

from pydantic import AwareDatetime, Field, model_validator

from app.domain.base import DomainModel, Identifier, MoneyRange, SignedRatio, UnitInterval
from app.domain.enums import SuggestionLabel
from app.domain.evidence import Evidence, EvidenceScore

ADVISORY_DISCLAIMER = "仅供个人决策参考；系统不执行交易，所有实际操作由用户独立判断和完成。"


class RiskConstraints(DomainModel):
    maximum_position_loss_ratio: UnitInterval
    single_add_reference_range: MoneyRange | None = None
    long_horizon: bool
    maximum_topic_weight: UnitInterval = Field(default=1)
    target_topic_weight: UnitInterval = Field(default=1)
    maximum_reduce_fraction: UnitInterval = Field(default=0)
    minimum_holding_days_before_reduce: int = Field(default=0, ge=0, le=36_500)
    maximum_purchase_fee_rate: UnitInterval = Field(default=1)
    maximum_redemption_fee_rate: UnitInterval = Field(default=1)

    @model_validator(mode="after")
    def target_must_fit_within_maximum(self) -> RiskConstraints:
        if self.target_topic_weight > self.maximum_topic_weight:
            raise ValueError("target topic weight cannot exceed its maximum")
        return self


class PositionRiskSnapshot(DomainModel):
    asset_id: Identifier
    portfolio_weight: UnitInterval
    unrealized_return_ratio: SignedRatio
    loss_boundary_used: UnitInterval
    recurring_contribution_active: bool
    holding_period_days: int = Field(default=0, ge=0, le=36_500)
    holding_period_data_complete: bool = False
    purchase_fee_rate: UnitInterval | None = None
    redemption_fee_rate: UnitInterval | None = None
    snapshot_at: AwareDatetime


class DecisionContext(DomainModel):
    context_id: Identifier
    asset_id: Identifier
    topic_ids: tuple[Identifier, ...] = Field(min_length=1, max_length=50)
    risk_constraints: RiskConstraints
    position: PositionRiskSnapshot
    evidence: tuple[Evidence, ...] = Field(default_factory=tuple, max_length=1000)
    scores: tuple[EvidenceScore, ...] = Field(default_factory=tuple, max_length=1000)
    data_as_of: AwareDatetime
    pipeline_version: str = Field(min_length=1, max_length=120)

    @model_validator(mode="after")
    def scores_must_reference_supplied_evidence(self) -> DecisionContext:
        if self.position.asset_id != self.asset_id:
            raise ValueError("position and decision context assets must match")
        evidence_ids = {item.evidence_id for item in self.evidence}
        if any(score.evidence_id not in evidence_ids for score in self.scores):
            raise ValueError("every score must reference supplied evidence")
        if len({item.evidence_id for item in self.evidence}) != len(self.evidence):
            raise ValueError("decision evidence ids must be unique")
        if len({item.evidence_id for item in self.scores}) != len(self.scores):
            raise ValueError("decision evidence score ids must be unique")
        return self


class DecisionResult(DomainModel):
    decision_id: Identifier
    context_id: Identifier
    label: SuggestionLabel
    strength: UnitInterval
    reasons: tuple[str, ...] = Field(min_length=1, max_length=50)
    evidence_ids: tuple[Identifier, ...] = Field(default_factory=tuple, max_length=1000)
    reference_amount: MoneyRange | None = None
    reference_reduce_fraction: UnitInterval | None = None
    allowed_labels: tuple[SuggestionLabel, ...] = Field(min_length=1, max_length=20)
    rule_version: str = Field(min_length=1, max_length=120)
    decided_at: AwareDatetime
    advisory_only: Literal[True] = True
    disclaimer: Literal["仅供个人决策参考；系统不执行交易，所有实际操作由用户独立判断和完成。"] = (
        ADVISORY_DISCLAIMER
    )

    @model_validator(mode="after")
    def validate_reference_amount(self) -> DecisionResult:
        if self.reference_amount is not None and self.label is not SuggestionLabel.SMALL_ADD:
            raise ValueError("only SMALL_ADD may include an add amount reference")
        if self.reference_reduce_fraction is not None and self.label not in {
            SuggestionLabel.REDUCE,
            SuggestionLabel.REBALANCE,
        }:
            raise ValueError("only REDUCE or REBALANCE may include a reduce fraction reference")
        if self.reference_reduce_fraction == 0:
            raise ValueError("a reduce fraction reference must be positive")
        if self.label is SuggestionLabel.INSUFFICIENT_DATA and self.strength != 0:
            raise ValueError("insufficient data must have zero suggestion strength")
        if self.label not in self.allowed_labels:
            raise ValueError("decision label must be inside the deterministic allowed set")
        if len(set(self.allowed_labels)) != len(self.allowed_labels):
            raise ValueError("allowed decision labels must be unique")
        return self


class AnalysisResult(DomainModel):
    analysis_id: Identifier
    context_id: Identifier
    bullish_factors: tuple[str, ...] = Field(default_factory=tuple, max_length=100)
    bearish_factors: tuple[str, ...] = Field(default_factory=tuple, max_length=100)
    uncertainties: tuple[str, ...] = Field(default_factory=tuple, max_length=100)
    evidence_ids: tuple[Identifier, ...] = Field(default_factory=tuple, max_length=1000)
    confidence: UnitInterval
    pipeline_version: str = Field(min_length=1, max_length=120)
    model_version: str = Field(min_length=1, max_length=120)
    prompt_version: str = Field(min_length=1, max_length=120)
    analyzed_at: AwareDatetime


class Report(DomainModel):
    report_id: Identifier
    asset_id: Identifier
    topic_ids: tuple[Identifier, ...] = Field(min_length=1, max_length=50)
    decision: DecisionResult
    analysis: AnalysisResult
    source_ids: tuple[Identifier, ...] = Field(default_factory=tuple, max_length=1000)
    evidence_ids: tuple[Identifier, ...] = Field(default_factory=tuple, max_length=1000)
    generated_at: AwareDatetime
    pipeline_version: str = Field(min_length=1, max_length=120)
    rule_version: str = Field(min_length=1, max_length=120)
    prompt_version: str = Field(min_length=1, max_length=120)
    advisory_only: Literal[True] = True
    disclaimer: Literal["仅供个人决策参考；系统不执行交易，所有实际操作由用户独立判断和完成。"] = (
        ADVISORY_DISCLAIMER
    )

    @model_validator(mode="after")
    def versions_and_evidence_must_be_consistent(self) -> Report:
        if self.decision.context_id != self.analysis.context_id:
            raise ValueError("decision and analysis contexts must match")
        if self.decision.rule_version != self.rule_version:
            raise ValueError("report and decision rule versions must match")
        if self.analysis.prompt_version != self.prompt_version:
            raise ValueError("report and analysis prompt versions must match")
        if self.analysis.pipeline_version != self.pipeline_version:
            raise ValueError("report and analysis pipeline versions must match")
        known_evidence = set(self.evidence_ids)
        if len(known_evidence) != len(self.evidence_ids):
            raise ValueError("report evidence ids must be unique")
        if not set(self.decision.evidence_ids).issubset(known_evidence):
            raise ValueError("decision evidence must be listed by the report")
        if not set(self.analysis.evidence_ids).issubset(known_evidence):
            raise ValueError("analysis evidence must be listed by the report")
        return self
