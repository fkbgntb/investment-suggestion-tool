"""Advisory-only decision, analysis, and report models."""

from __future__ import annotations

from decimal import Decimal
from typing import Literal

from pydantic import AnyHttpUrl, AwareDatetime, Field, model_validator

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


class CausalChainStep(DomainModel):
    evidence_id: Identifier
    relation: str = Field(min_length=1, max_length=500)
    confidence: UnitInterval


class CausalChain(DomainModel):
    steps: tuple[CausalChainStep, ...] = Field(min_length=1, max_length=10)
    conclusion: str = Field(min_length=1, max_length=1000)
    confidence: UnitInterval

    @model_validator(mode="after")
    def chain_confidence_cannot_exceed_weakest_step(self) -> CausalChain:
        if self.confidence > min(step.confidence for step in self.steps):
            raise ValueError("causal-chain confidence cannot exceed its weakest step")
        return self


class AnalysisResult(DomainModel):
    analysis_id: Identifier
    context_id: Identifier
    stance: Literal["BULLISH", "BEARISH", "MIXED", "UNCERTAIN"] = "UNCERTAIN"
    summary: str = Field(default="暂无综合摘要", min_length=1, max_length=4000)
    bullish_factors: tuple[str, ...] = Field(default_factory=tuple, max_length=100)
    bearish_factors: tuple[str, ...] = Field(default_factory=tuple, max_length=100)
    uncertainties: tuple[str, ...] = Field(default_factory=tuple, max_length=100)
    bullish_evidence_ids: tuple[Identifier, ...] = Field(default_factory=tuple, max_length=1000)
    bearish_evidence_ids: tuple[Identifier, ...] = Field(default_factory=tuple, max_length=1000)
    neutral_evidence_ids: tuple[Identifier, ...] = Field(default_factory=tuple, max_length=1000)
    causal_chains: tuple[CausalChain, ...] = Field(default_factory=tuple, max_length=100)
    invalidation_triggers: tuple[str, ...] = Field(default_factory=tuple, max_length=100)
    suggested_action: SuggestionLabel = SuggestionLabel.OBSERVE
    allowed_actions: tuple[SuggestionLabel, ...] = Field(min_length=1, max_length=20)
    evidence_ids: tuple[Identifier, ...] = Field(default_factory=tuple, max_length=1000)
    confidence: UnitInterval
    pipeline_version: str = Field(min_length=1, max_length=120)
    model_version: str = Field(min_length=1, max_length=120)
    prompt_version: str = Field(min_length=1, max_length=120)
    provider_name: Identifier = "rule-synthesis"
    degraded: bool = False
    input_tokens: int = Field(default=0, ge=0)
    output_tokens: int = Field(default=0, ge=0)
    analyzed_at: AwareDatetime

    @model_validator(mode="after")
    def references_and_action_must_be_bounded(self) -> AnalysisResult:
        known = set(self.evidence_ids)
        referenced = {
            *self.bullish_evidence_ids,
            *self.bearish_evidence_ids,
            *self.neutral_evidence_ids,
        }
        for chain in self.causal_chains:
            referenced.update(step.evidence_id for step in chain.steps)
        if not referenced.issubset(known):
            raise ValueError("analysis references unknown evidence ids")
        if self.suggested_action not in self.allowed_actions:
            raise ValueError("AI suggestion must remain inside the deterministic allowed set")
        if len(set(self.allowed_actions)) != len(self.allowed_actions):
            raise ValueError("analysis allowed actions must be unique")
        return self


class ReportSource(DomainModel):
    evidence_id: Identifier
    source_id: Identifier
    title: str = Field(min_length=1, max_length=1000)
    url: AnyHttpUrl
    health_status: str = Field(default="UNKNOWN", min_length=1, max_length=64)


class ReportDifference(DomainModel):
    older_report_id: Identifier
    newer_report_id: Identifier
    decision_changed: bool
    older_label: SuggestionLabel
    newer_label: SuggestionLabel
    confidence_change: Decimal = Field(ge=Decimal("-1"), le=Decimal("1"))
    added_evidence_ids: tuple[Identifier, ...] = Field(default_factory=tuple, max_length=1000)
    removed_evidence_ids: tuple[Identifier, ...] = Field(default_factory=tuple, max_length=1000)


class Report(DomainModel):
    report_id: Identifier
    asset_id: Identifier
    topic_ids: tuple[Identifier, ...] = Field(min_length=1, max_length=50)
    decision: DecisionResult
    analysis: AnalysisResult
    source_ids: tuple[Identifier, ...] = Field(default_factory=tuple, max_length=1000)
    evidence_ids: tuple[Identifier, ...] = Field(default_factory=tuple, max_length=1000)
    sources: tuple[ReportSource, ...] = Field(default_factory=tuple, max_length=1000)
    data_as_of: AwareDatetime
    data_is_stale: bool
    stale_after_hours: int = Field(default=30, ge=1, le=24 * 30)
    generated_at: AwareDatetime
    pipeline_version: str = Field(min_length=1, max_length=120)
    rule_version: str = Field(min_length=1, max_length=120)
    prompt_version: str = Field(min_length=1, max_length=120)
    template_version: str = Field(min_length=1, max_length=120)
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
        if any(source.evidence_id not in known_evidence for source in self.sources):
            raise ValueError("report source references unknown evidence")
        if any(source.source_id not in set(self.source_ids) for source in self.sources):
            raise ValueError("report source link must use a listed source id")
        if self.generated_at < self.data_as_of:
            raise ValueError("report cannot be generated before its data timestamp")
        return self
