"""Personal-MVP orchestration from a local position snapshot to a rendered report."""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from uuid import NAMESPACE_URL, uuid5

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.config import Settings
from app.domain.analysis import DecisionContext, PositionRiskSnapshot, RiskConstraints
from app.domain.base import Money
from app.domain.enums import FeeDataStatus
from app.domain.evidence import Evidence, EvidenceScore
from app.services.analysis_synthesis import AnalysisSynthesisService, build_synthesis_provider
from app.services.decision import DecisionRunService
from app.services.portfolio import PortfolioService
from app.services.reports import ReportService
from app.storage.models import (
    AnalysisResultRow,
    DecisionResultRow,
    EvidenceItemRow,
    EvidenceScoreRow,
    ReportRow,
)


class AnalysisWorkflowError(RuntimeError):
    pass


class AnalysisWorkflowService:
    def __init__(
        self,
        session: Session,
        workspace_id: str,
        settings: Settings,
    ) -> None:
        self.session = session
        self.workspace_id = workspace_id
        self.settings = settings

    async def run(
        self,
        *,
        position_id: str,
        portfolio_value: Money,
        now: datetime,
        maximum_topic_weight: Decimal = Decimal("0.35"),
        target_topic_weight: Decimal = Decimal("0.25"),
    ) -> tuple[DecisionResultRow, AnalysisResultRow, ReportRow]:
        if now.tzinfo is None:
            raise AnalysisWorkflowError("analysis time must include a timezone")
        portfolio = PortfolioService(self.session, self.workspace_id)
        position = portfolio.get_position(position_id)
        profile = portfolio.get_profile(position.profile_id)
        asset = portfolio.get_asset(position.asset_id)
        if portfolio_value.currency != position.current_value.currency:
            raise AnalysisWorkflowError("portfolio and position currencies must match")
        if portfolio_value.amount < position.current_value.amount:
            raise AnalysisWorkflowError("portfolio value cannot be smaller than this position")
        if target_topic_weight > maximum_topic_weight:
            raise AnalysisWorkflowError("target topic weight cannot exceed its maximum")

        snapshot = portfolio.create_analysis_snapshot(position_id, generated_at=now)
        evidence, scores = self._latest_evidence()
        data_as_of = max((score.scored_at for score in scores), default=now)
        cost = position.cost_basis.amount
        current = position.current_value.amount
        unrealized = (current - cost) / cost
        loss = max(cost - current, Decimal("0"))
        boundary = profile.fund_reanalysis_threshold.amount
        loss_used = min(Decimal("1"), loss / boundary) if boundary else Decimal(int(loss > 0))
        holding_days = max(0, (now.date() - position.opened_on).days)
        purchase_fee = None
        redemption_fee = None
        if asset.fee_policy.status is not FeeDataStatus.UNKNOWN:
            purchase_fee = asset.fee_policy.purchase_fee_rate
            if position.holding_period_data_complete:
                redemption_fee = next(
                    (
                        tier.fee_rate
                        for tier in asset.fee_policy.redemption_fee_tiers
                        if holding_days >= tier.minimum_holding_days
                        and (
                            tier.maximum_holding_days is None
                            or holding_days <= tier.maximum_holding_days
                        )
                    ),
                    None,
                )
        context_id = str(
            uuid5(
                NAMESPACE_URL,
                f"{snapshot.snapshot_id}:{data_as_of.isoformat()}:"
                f"{','.join(score.evidence_id for score in scores)}",
            )
        )
        context = DecisionContext(
            context_id=context_id,
            asset_id=position.asset_id,
            topic_ids=tuple(sorted({topic for item in evidence for topic in item.draft.topic_ids}))
            or ("semiconductor",),
            risk_constraints=RiskConstraints(
                maximum_position_loss_ratio=Decimal("1"),
                single_add_reference_range=profile.single_add_range,
                long_horizon=profile.accepts_long_term_volatility,
                maximum_topic_weight=maximum_topic_weight,
                target_topic_weight=target_topic_weight,
                maximum_reduce_fraction=Decimal("0.10"),
                minimum_holding_days_before_reduce=30,
                maximum_purchase_fee_rate=Decimal("0.01"),
                maximum_redemption_fee_rate=Decimal("0.01"),
            ),
            position=PositionRiskSnapshot(
                asset_id=position.asset_id,
                portfolio_weight=current / portfolio_value.amount,
                unrealized_return_ratio=max(Decimal("-1"), min(Decimal("1"), unrealized)),
                loss_boundary_used=loss_used,
                recurring_contribution_active=position.recurring_contribution is not None,
                holding_period_days=holding_days,
                holding_period_data_complete=position.holding_period_data_complete,
                purchase_fee_rate=purchase_fee,
                redemption_fee_rate=redemption_fee,
                snapshot_at=snapshot.generated_at,
            ),
            evidence=evidence,
            scores=scores,
            data_as_of=data_as_of,
            pipeline_version="personal-mvp-1.0.0",
        )
        decision = DecisionRunService(self.session, self.workspace_id).run(
            context,
            position_snapshot_id=snapshot.snapshot_id,
            now=now,
        )
        decision_row = self.session.scalar(
            select(DecisionResultRow).where(
                DecisionResultRow.workspace_id == self.workspace_id,
                DecisionResultRow.decision_id == decision.decision_id,
            )
        )
        if decision_row is None:
            raise AnalysisWorkflowError("decision persistence failed")
        provider = build_synthesis_provider(self.settings)
        await AnalysisSynthesisService(
            self.session,
            self.workspace_id,
            provider,
            model_version=(
                self.settings.deepseek_model
                if self.settings.deepseek_api_key is not None
                else "rules-1.0.0"
            ),
            max_calls_per_day=self.settings.deepseek_synthesis_max_calls_per_day,
            daily_token_budget=self.settings.deepseek_synthesis_daily_token_budget,
        ).synthesize_pending(now=now)
        await ReportService(self.session, self.workspace_id).generate_pending(now=now)
        analysis_row = self.session.scalar(
            select(AnalysisResultRow).where(
                AnalysisResultRow.workspace_id == self.workspace_id,
                AnalysisResultRow.analysis_run_id == decision_row.analysis_run_id,
            )
        )
        report_row = self.session.scalar(
            select(ReportRow).where(
                ReportRow.workspace_id == self.workspace_id,
                ReportRow.analysis_run_id == decision_row.analysis_run_id,
            )
        )
        if analysis_row is None or report_row is None:
            raise AnalysisWorkflowError("analysis report pipeline did not complete")
        return decision_row, analysis_row, report_row

    def _latest_evidence(
        self, *, limit: int = 100
    ) -> tuple[tuple[Evidence, ...], tuple[EvidenceScore, ...]]:
        rows = self.session.execute(
            select(EvidenceItemRow, EvidenceScoreRow)
            .join(
                EvidenceScoreRow,
                (EvidenceScoreRow.workspace_id == EvidenceItemRow.workspace_id)
                & (EvidenceScoreRow.evidence_id == EvidenceItemRow.evidence_id),
            )
            .where(EvidenceItemRow.workspace_id == self.workspace_id)
            .order_by(EvidenceScoreRow.created_at.desc(), EvidenceItemRow.evidence_id)
        ).all()
        latest: dict[str, tuple[Evidence, EvidenceScore]] = {}
        for evidence_row, score_row in rows:
            if evidence_row.evidence_id in latest:
                continue
            latest[evidence_row.evidence_id] = (
                Evidence.model_validate(evidence_row.payload),
                EvidenceScore.model_validate(score_row.payload),
            )
            if len(latest) >= limit:
                break
        values = tuple(latest.values())
        return tuple(item[0] for item in values), tuple(item[1] for item in values)
