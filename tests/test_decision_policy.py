from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path

from sqlalchemy import select

from app.domain.analysis import (
    DecisionContext,
    PositionRiskSnapshot,
    RiskConstraints,
)
from app.domain.base import Money, MoneyRange
from app.domain.enums import (
    EvidenceDirection,
    SourceKind,
    SuggestionLabel,
    TrustTier,
)
from app.domain.evidence import Evidence, EvidenceDraft, EvidenceScore
from app.services.decision import (
    DecisionPolicyParameters,
    DecisionRunService,
    DeterministicDecisionPolicy,
)
from app.services.portfolio import PortfolioService
from app.storage.models import AnalysisRunRow, DecisionResultRow
from tests.domain_factories import investment_profile
from tests.test_portfolio_service import asset, database, position

NOW = datetime(2026, 7, 20, 12, tzinfo=UTC)


def money_range() -> MoneyRange:
    return MoneyRange(
        minimum=Money(amount=Decimal("100"), currency="CNY"),
        maximum=Money(amount=Decimal("200"), currency="CNY"),
    )


def item(index: int, direction: EvidenceDirection) -> tuple[Evidence, EvidenceScore]:
    evidence_id = f"evidence-decision-{index}"
    return (
        Evidence(
            evidence_id=evidence_id,
            document_id=f"document-decision-{index}",
            draft=EvidenceDraft(
                claim=f"claim {index}",
                direction=direction,
                quote=f"quote {index}",
                topic_ids=("semiconductor",),
                confidence=Decimal("0.8"),
                claim_type="demand",
                impact_horizon="MEDIUM",
                directness=Decimal("0.8"),
            ),
            extracted_at=NOW,
            extractor_name="mock-ai",
            model_version="mock-v1",
            prompt_version="prompt-v1",
        ),
        EvidenceScore(
            evidence_id=evidence_id,
            source_quality=Decimal("0.8"),
            independence=Decimal("0.75"),
            recency=Decimal("1"),
            relevance=Decimal("0.9"),
            directness=Decimal("0.8"),
            extraction_confidence=Decimal("0.8"),
            total=Decimal("0.20"),
            source_kind=SourceKind.RESEARCH,
            trust_tier=TrustTier.PROFESSIONAL,
            independent_source_count=2,
            scoring_version="evidence-score-1.0.0",
            scored_at=NOW,
        ),
    )


def context(
    directions: tuple[EvidenceDirection, ...] = (
        EvidenceDirection.POSITIVE,
        EvidenceDirection.POSITIVE,
    ),
    *,
    data_as_of: datetime = NOW,
    weight: str = "0.20",
    loss_used: str = "0",
    holding_complete: bool = True,
    purchase_fee: Decimal | None = Decimal("0.001"),
    redemption_fee: Decimal | None = Decimal("0.005"),
) -> DecisionContext:
    pairs = tuple(item(index, direction) for index, direction in enumerate(directions, start=1))
    return DecisionContext(
        context_id="decision-context-1",
        asset_id="asset-007300",
        topic_ids=("semiconductor",),
        risk_constraints=RiskConstraints(
            maximum_position_loss_ratio=Decimal("1"),
            single_add_reference_range=money_range(),
            long_horizon=True,
            maximum_topic_weight=Decimal("0.40"),
            target_topic_weight=Decimal("0.30"),
            maximum_reduce_fraction=Decimal("0.10"),
            minimum_holding_days_before_reduce=30,
            maximum_purchase_fee_rate=Decimal("0.01"),
            maximum_redemption_fee_rate=Decimal("0.01"),
        ),
        position=PositionRiskSnapshot(
            asset_id="asset-007300",
            portfolio_weight=Decimal(weight),
            unrealized_return_ratio=Decimal("0.0212"),
            loss_boundary_used=Decimal(loss_used),
            recurring_contribution_active=True,
            holding_period_days=100,
            holding_period_data_complete=holding_complete,
            purchase_fee_rate=purchase_fee,
            redemption_fee_rate=redemption_fee,
            snapshot_at=NOW,
        ),
        evidence=tuple(pair[0] for pair in pairs),
        scores=tuple(pair[1] for pair in pairs),
        data_as_of=data_as_of,
        pipeline_version="pipeline-v1",
    )


def evaluate(value: DecisionContext):
    return DeterministicDecisionPolicy(decided_at=NOW).evaluate(value)


def test_stale_or_insufficient_data_never_returns_an_action() -> None:
    stale = evaluate(context(data_as_of=NOW - timedelta(hours=31)))
    sparse = evaluate(context((EvidenceDirection.POSITIVE,)))
    assert stale.label is SuggestionLabel.INSUFFICIENT_DATA
    assert sparse.label is SuggestionLabel.INSUFFICIENT_DATA
    assert stale.strength == sparse.strength == 0
    assert SuggestionLabel.SMALL_ADD not in stale.allowed_labels
    assert SuggestionLabel.REDUCE not in sparse.allowed_labels


def test_two_independent_positive_sources_allow_only_bounded_small_add() -> None:
    result = evaluate(context())
    assert result.label is SuggestionLabel.SMALL_ADD
    assert result.reference_amount == money_range()
    assert result.advisory_only is True
    assert SuggestionLabel.REDUCE not in result.allowed_labels


def test_concentration_loss_and_unknown_fee_cannot_be_overridden() -> None:
    concentrated = evaluate(context(weight="0.40"))
    loss_boundary = evaluate(context(loss_used="1"))
    fee_unknown = evaluate(context(purchase_fee=None))
    assert concentrated.label is SuggestionLabel.PAUSE_ADDING
    assert loss_boundary.label is SuggestionLabel.PAUSE_ADDING
    assert fee_unknown.label is SuggestionLabel.OBSERVE
    for result in (concentrated, loss_boundary, fee_unknown):
        assert SuggestionLabel.SMALL_ADD not in result.allowed_labels


def test_reduce_requires_complete_holding_and_fee_data_and_is_capped() -> None:
    negative = (EvidenceDirection.NEGATIVE, EvidenceDirection.NEGATIVE)
    incomplete = evaluate(context(negative, holding_complete=False))
    unknown_fee = evaluate(context(negative, redemption_fee=None))
    permitted = evaluate(context(negative))
    assert incomplete.label is SuggestionLabel.PAUSE_ADDING
    assert unknown_fee.label is SuggestionLabel.PAUSE_ADDING
    assert permitted.label is SuggestionLabel.REDUCE
    assert permitted.reference_reduce_fraction == Decimal("0.10")
    assert permitted.advisory_only is True


def test_sentiment_or_weak_independence_only_observes() -> None:
    value = context()
    social_scores = tuple(
        score.model_copy(
            update={
                "source_kind": SourceKind.SOCIAL,
                "trust_tier": TrustTier.SENTIMENT_ONLY,
                "total": Decimal("0.10"),
            }
        )
        for score in value.scores
    )
    social = evaluate(value.model_copy(update={"scores": social_scores}))
    strict = DeterministicDecisionPolicy(
        decided_at=NOW,
        parameters=DecisionPolicyParameters(minimum_independent_high_quality=3),
    ).evaluate(value)
    assert social.label is SuggestionLabel.OBSERVE
    assert strict.label is SuggestionLabel.OBSERVE


def test_decision_run_persists_versions_and_immutable_input(tmp_path: Path) -> None:
    db = database(tmp_path)
    try:
        with db.session() as session:
            portfolio = PortfolioService(session, "personal-demo")
            portfolio.create_profile(investment_profile())
            portfolio.create_asset(asset())
            portfolio.create_position(position())
            snapshot = portfolio.create_analysis_snapshot("position-007300", generated_at=NOW)
            first = DecisionRunService(session, "personal-demo").run(
                context(), position_snapshot_id=snapshot.snapshot_id, now=NOW
            )
            second = DecisionRunService(session, "personal-demo").run(
                context(), position_snapshot_id=snapshot.snapshot_id, now=NOW
            )
            run = session.scalar(select(AnalysisRunRow))
            decision = session.scalar(select(DecisionResultRow))
            assert first == second
            assert run is not None and decision is not None
            assert run.rule_version == "decision-policy-1.0.0"
            assert run.scoring_version == "evidence-score-1.0.0"
            assert (
                run.input_snapshot["context"]["risk_constraints"]["maximum_topic_weight"] == "0.40"
            )
            assert run.input_snapshot["rule_version"] == "decision-policy-1.0.0"
            assert decision.payload["advisory_only"] is True
    finally:
        db.dispose()
