"""Deterministic, advisory-only investment decision policy and persistence."""

from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime
from decimal import ROUND_HALF_UP, Decimal
from uuid import NAMESPACE_URL, uuid5

from pydantic import Field
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.domain.analysis import DecisionContext, DecisionResult
from app.domain.base import DomainModel, Money, MoneyRange, UnitInterval
from app.domain.enums import EvidenceDirection, SourceKind, SuggestionLabel, TrustTier
from app.storage.models import AnalysisRunRow, DecisionResultRow

DECISION_RULE_VERSION = "decision-policy-1.0.0"
_ACTIONABLE_TRUST = {TrustTier.PRIMARY, TrustTier.PROFESSIONAL}
_SENTIMENT_KINDS = {SourceKind.SOCIAL, SourceKind.COMMUNITY}
_FOUR_PLACES = Decimal("0.0001")


class DecisionPolicyParameters(DomainModel):
    maximum_data_age_hours: int = Field(default=30, ge=1, le=24 * 30)
    minimum_scored_evidence: int = Field(default=2, ge=1, le=100)
    minimum_independent_high_quality: int = Field(default=2, ge=1, le=20)
    high_quality_score_threshold: UnitInterval = Decimal("0.15")
    minimum_signal_margin: UnitInterval = Decimal("0.08")


def _unit(value: Decimal) -> Decimal:
    return min(Decimal("1"), max(Decimal("0"), value)).quantize(
        _FOUR_PLACES, rounding=ROUND_HALF_UP
    )


class DeterministicDecisionPolicy:
    """Apply local risk boundaries before any synthesis model is consulted."""

    rule_version = DECISION_RULE_VERSION

    def __init__(
        self,
        *,
        decided_at: datetime,
        parameters: DecisionPolicyParameters | None = None,
    ) -> None:
        if decided_at.tzinfo is None:
            raise ValueError("decision time must include a timezone")
        self.decided_at = decided_at.astimezone(UTC)
        self.parameters = parameters or DecisionPolicyParameters()

    def evaluate(self, context: DecisionContext) -> DecisionResult:
        scores = {item.evidence_id: item for item in context.scores}
        evidence = [item for item in context.evidence if item.evidence_id in scores]
        all_ids = tuple(item.evidence_id for item in evidence)
        stale_hours = max(
            Decimal("0"),
            Decimal(
                str((self.decided_at - context.data_as_of.astimezone(UTC)).total_seconds() / 3600)
            ),
        )
        if stale_hours > self.parameters.maximum_data_age_hours:
            return self._result(
                context,
                SuggestionLabel.INSUFFICIENT_DATA,
                Decimal("0"),
                (f"数据已超过 {self.parameters.maximum_data_age_hours} 小时，暂不产生操作建议",),
                all_ids,
                (SuggestionLabel.INSUFFICIENT_DATA, SuggestionLabel.OBSERVE),
            )
        if len(evidence) < self.parameters.minimum_scored_evidence:
            return self._result(
                context,
                SuggestionLabel.INSUFFICIENT_DATA,
                Decimal("0"),
                (
                    f"仅有 {len(evidence)} 条已评分证据，少于最低要求 "
                    f"{self.parameters.minimum_scored_evidence} 条",
                ),
                all_ids,
                (SuggestionLabel.INSUFFICIENT_DATA, SuggestionLabel.OBSERVE),
            )

        high_quality = [
            item
            for item in evidence
            if scores[item.evidence_id].total >= self.parameters.high_quality_score_threshold
            and scores[item.evidence_id].trust_tier in _ACTIONABLE_TRUST
            and scores[item.evidence_id].independence > 0
            and not scores[item.evidence_id].same_origin_reprint
        ]
        actionable_ids = tuple(item.evidence_id for item in high_quality)
        if all(scores[item.evidence_id].source_kind in _SENTIMENT_KINDS for item in evidence):
            return self._result(
                context,
                SuggestionLabel.OBSERVE,
                Decimal("0"),
                ("现有信息全部来自社交或社区情绪，不能单独触发仓位建议",),
                all_ids,
                (SuggestionLabel.OBSERVE,),
            )
        if len(high_quality) < self.parameters.minimum_independent_high_quality:
            return self._result(
                context,
                SuggestionLabel.OBSERVE,
                Decimal("0"),
                (
                    f"只有 {len(high_quality)} 条独立高质量证据，尚未达到动作门槛 "
                    f"{self.parameters.minimum_independent_high_quality} 条",
                ),
                all_ids,
                (SuggestionLabel.OBSERVE, SuggestionLabel.HOLD),
            )

        positive, negative = self._directional_totals(high_quality, scores)
        signal_total = positive + negative
        margin = abs(positive - negative) / signal_total if signal_total else Decimal("0")
        signal_reason = (
            f"高质量证据加权：利多 {positive:.4f}，利空 {negative:.4f}，差异 {margin:.4f}"
        )
        if signal_total == 0 or margin < self.parameters.minimum_signal_margin:
            return self._result(
                context,
                SuggestionLabel.HOLD,
                _unit(margin),
                (signal_reason, "多空差异未达到确定性动作门槛"),
                actionable_ids,
                (SuggestionLabel.OBSERVE, SuggestionLabel.HOLD),
            )

        if positive > negative:
            return self._positive_result(context, margin, signal_reason, actionable_ids)
        return self._negative_result(context, margin, signal_reason, actionable_ids)

    @staticmethod
    def _directional_totals(evidence: list, scores: dict) -> tuple[Decimal, Decimal]:
        positive = Decimal("0")
        negative = Decimal("0")
        for item in evidence:
            weight = scores[item.evidence_id].total
            if item.draft.direction is EvidenceDirection.POSITIVE:
                positive += weight
            elif item.draft.direction is EvidenceDirection.NEGATIVE:
                negative += weight
            elif item.draft.direction is EvidenceDirection.MIXED:
                positive += weight / 2
                negative += weight / 2
        return positive, negative

    def _positive_result(
        self,
        context: DecisionContext,
        margin: Decimal,
        signal_reason: str,
        evidence_ids: tuple[str, ...],
    ) -> DecisionResult:
        position = context.position
        risk = context.risk_constraints
        if position.loss_boundary_used >= risk.maximum_position_loss_ratio:
            return self._result(
                context,
                SuggestionLabel.PAUSE_ADDING,
                _unit(margin),
                (signal_reason, "单基金亏损边界已被触及，不允许扩大风险敞口"),
                evidence_ids,
                (SuggestionLabel.OBSERVE, SuggestionLabel.HOLD, SuggestionLabel.PAUSE_ADDING),
            )
        if position.portfolio_weight >= risk.maximum_topic_weight:
            return self._result(
                context,
                SuggestionLabel.PAUSE_ADDING,
                _unit(margin),
                (signal_reason, "当前主题仓位已达到上限，不建议继续加仓"),
                evidence_ids,
                (SuggestionLabel.OBSERVE, SuggestionLabel.HOLD, SuggestionLabel.PAUSE_ADDING),
            )
        if position.portfolio_weight >= risk.target_topic_weight:
            return self._result(
                context,
                SuggestionLabel.HOLD,
                _unit(margin),
                (signal_reason, "当前主题仓位已达到目标区间"),
                evidence_ids,
                (SuggestionLabel.OBSERVE, SuggestionLabel.HOLD, SuggestionLabel.PAUSE_ADDING),
            )
        if risk.single_add_reference_range is None:
            return self._result(
                context,
                SuggestionLabel.OBSERVE,
                Decimal("0"),
                (signal_reason, "尚未配置单次加仓参考区间"),
                evidence_ids,
                (SuggestionLabel.OBSERVE, SuggestionLabel.HOLD),
            )
        if (
            position.purchase_fee_rate is None
            or position.purchase_fee_rate > risk.maximum_purchase_fee_rate
        ):
            return self._result(
                context,
                SuggestionLabel.OBSERVE,
                Decimal("0"),
                (signal_reason, "申购费率未知或超过本地费率上限，暂不提供加仓金额"),
                evidence_ids,
                (SuggestionLabel.OBSERVE, SuggestionLabel.HOLD),
            )
        return self._result(
            context,
            SuggestionLabel.SMALL_ADD,
            _unit(margin),
            (signal_reason, "风险、仓位和申购费率均处于本地允许范围"),
            evidence_ids,
            (
                SuggestionLabel.OBSERVE,
                SuggestionLabel.HOLD,
                SuggestionLabel.PAUSE_ADDING,
                SuggestionLabel.SMALL_ADD,
            ),
            reference_amount=risk.single_add_reference_range,
        )

    def _negative_result(
        self,
        context: DecisionContext,
        margin: Decimal,
        signal_reason: str,
        evidence_ids: tuple[str, ...],
    ) -> DecisionResult:
        position = context.position
        risk = context.risk_constraints
        common_allowed = (
            SuggestionLabel.OBSERVE,
            SuggestionLabel.HOLD,
            SuggestionLabel.PAUSE_ADDING,
        )
        if not position.holding_period_data_complete:
            return self._result(
                context,
                SuggestionLabel.PAUSE_ADDING
                if position.recurring_contribution_active
                else SuggestionLabel.OBSERVE,
                _unit(margin),
                (signal_reason, "持有期数据不完整，不提供减仓比例"),
                evidence_ids,
                common_allowed,
            )
        if position.holding_period_days < risk.minimum_holding_days_before_reduce:
            return self._result(
                context,
                SuggestionLabel.PAUSE_ADDING
                if position.recurring_contribution_active
                else SuggestionLabel.OBSERVE,
                _unit(margin),
                (signal_reason, "持有期尚未达到本地减仓复核门槛"),
                evidence_ids,
                common_allowed,
            )
        if (
            position.redemption_fee_rate is None
            or position.redemption_fee_rate > risk.maximum_redemption_fee_rate
            or risk.maximum_reduce_fraction == 0
        ):
            return self._result(
                context,
                SuggestionLabel.PAUSE_ADDING
                if position.recurring_contribution_active
                else SuggestionLabel.OBSERVE,
                _unit(margin),
                (signal_reason, "赎回费率未知、过高或未配置减仓上限，不提供减仓比例"),
                evidence_ids,
                common_allowed,
            )
        return self._result(
            context,
            SuggestionLabel.REDUCE,
            _unit(margin),
            (signal_reason, "满足独立证据、持有期、费用和本地减仓边界"),
            evidence_ids,
            (*common_allowed, SuggestionLabel.REDUCE),
            reference_reduce_fraction=risk.maximum_reduce_fraction,
        )

    def _result(
        self,
        context: DecisionContext,
        label: SuggestionLabel,
        strength: Decimal,
        reasons: tuple[str, ...],
        evidence_ids: tuple[str, ...],
        allowed_labels: tuple[SuggestionLabel, ...],
        *,
        reference_amount: MoneyRange | None = None,
        reference_reduce_fraction: Decimal | None = None,
    ) -> DecisionResult:
        decision_id = str(
            uuid5(
                NAMESPACE_URL,
                f"{context.context_id}:{self.rule_version}:{self.decided_at.isoformat()}",
            )
        )
        return DecisionResult(
            decision_id=decision_id,
            context_id=context.context_id,
            label=label,
            strength=_unit(strength),
            reasons=reasons,
            evidence_ids=evidence_ids,
            reference_amount=reference_amount,
            reference_reduce_fraction=reference_reduce_fraction,
            allowed_labels=allowed_labels,
            rule_version=self.rule_version,
            decided_at=self.decided_at,
        )


class DecisionRunService:
    """Persist an immutable decision input snapshot and deterministic result."""

    def __init__(self, session: Session, workspace_id: str) -> None:
        self.session = session
        self.workspace_id = workspace_id

    def run(
        self,
        context: DecisionContext,
        *,
        position_snapshot_id: str,
        now: datetime,
        parameters: DecisionPolicyParameters | None = None,
        idempotency_key_override: str | None = None,
    ) -> DecisionResult:
        policy = DeterministicDecisionPolicy(decided_at=now, parameters=parameters)
        scoring_versions = sorted({score.scoring_version for score in context.scores})
        scoring_version = ",".join(scoring_versions) or "none"
        if len(scoring_version) > 120:
            raise ValueError("combined scoring versions exceed storage boundary")
        input_snapshot = {
            "context": context.model_dump(mode="json"),
            "parameters": policy.parameters.model_dump(mode="json"),
            "decided_at": policy.decided_at.isoformat(),
            "rule_version": policy.rule_version,
        }
        digest = hashlib.sha256(
            json.dumps(
                input_snapshot, ensure_ascii=False, sort_keys=True, separators=(",", ":")
            ).encode("utf-8")
        ).hexdigest()
        idempotency_key = idempotency_key_override or f"decision-{digest[:48]}"
        if len(idempotency_key) > 128:
            raise ValueError("analysis idempotency key exceeds storage boundary")
        existing = self.session.scalar(
            select(AnalysisRunRow).where(
                AnalysisRunRow.workspace_id == self.workspace_id,
                AnalysisRunRow.idempotency_key == idempotency_key,
            )
        )
        if existing is not None:
            decision_row = self.session.scalar(
                select(DecisionResultRow).where(
                    DecisionResultRow.workspace_id == self.workspace_id,
                    DecisionResultRow.analysis_run_id == existing.analysis_run_id,
                )
            )
            if decision_row is None:
                raise RuntimeError("analysis run exists without its deterministic decision")
            return DecisionResult.model_validate(decision_row.payload)

        result = policy.evaluate(context)
        analysis_run_id = str(uuid5(NAMESPACE_URL, f"{self.workspace_id}:{idempotency_key}"))
        self.session.add(
            AnalysisRunRow(
                analysis_run_id=analysis_run_id,
                workspace_id=self.workspace_id,
                asset_id=context.asset_id,
                position_snapshot_id=position_snapshot_id,
                idempotency_key=idempotency_key,
                pipeline_version=context.pipeline_version,
                prompt_version="not-used",
                rule_version=result.rule_version,
                scoring_version=scoring_version,
                input_snapshot=input_snapshot,
                payload={
                    "context_id": context.context_id,
                    "decision_id": result.decision_id,
                    "status": "DECIDED",
                    "rule_version": result.rule_version,
                    "scoring_versions": scoring_versions,
                    "decided_at": result.decided_at.isoformat(),
                },
            )
        )
        self.session.add(
            DecisionResultRow(
                decision_id=result.decision_id,
                workspace_id=self.workspace_id,
                analysis_run_id=analysis_run_id,
                label=result.label.value,
                rule_version=result.rule_version,
                schema_version=result.schema_version,
                payload=result.model_dump(mode="json"),
            )
        )
        self.session.flush()
        return result


def cap_money_range(reference: MoneyRange, maximum: Money) -> MoneyRange | None:
    """Reserved helper for a future target-gap amount without increasing user limits."""

    if reference.minimum.currency != maximum.currency:
        raise ValueError("money range and cap currencies must match")
    capped_maximum = min(reference.maximum.amount, maximum.amount)
    if capped_maximum < reference.minimum.amount:
        return None
    return MoneyRange(
        minimum=reference.minimum,
        maximum=Money(amount=capped_maximum, currency=maximum.currency),
    )
