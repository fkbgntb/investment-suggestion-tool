"""Small valid object factories shared by domain contract tests."""

from datetime import UTC, date, datetime
from decimal import Decimal

from app.domain.analysis import (
    AnalysisResult,
    DecisionContext,
    DecisionResult,
    PositionRiskSnapshot,
    Report,
    RiskConstraints,
)
from app.domain.base import IdempotencyKey, Money, MoneyRange
from app.domain.enums import EvidenceDirection, SuggestionLabel
from app.domain.evidence import Evidence, EvidenceDraft, EvidenceScore
from app.domain.portfolio import InvestmentProfile

NOW = datetime(2026, 7, 19, 12, 0, tzinfo=UTC)
HASH = "a" * 64


def money(amount: str) -> Money:
    return Money(amount=Decimal(amount), currency="CNY")


def money_range(minimum: str = "100", maximum: str = "200") -> MoneyRange:
    return MoneyRange(minimum=money(minimum), maximum=money(maximum))


def idempotency() -> IdempotencyKey:
    return IdempotencyKey(scope="test", key="stable-key", payload_sha256=HASH)


def investment_profile() -> InvestmentProfile:
    return InvestmentProfile(
        profile_id="profile-demo",
        name="个人训练账户",
        maximum_portfolio_loss=money("1000"),
        fund_loss_warning=money("200"),
        fund_reanalysis_threshold=money("300"),
        single_add_range=money_range(),
        monthly_contribution=money("50"),
        accepts_long_term_volatility=True,
    )


def evidence() -> Evidence:
    return Evidence(
        evidence_id="evidence-1",
        document_id="document-1",
        draft=EvidenceDraft(
            claim="示例证据",
            direction=EvidenceDirection.NEUTRAL,
            topic_ids=("semiconductor",),
            confidence=Decimal("0.7"),
            claim_type="example",
            impact_horizon="UNKNOWN",
            directness=Decimal("0.5"),
        ),
        extracted_at=NOW,
        extractor_name="mock-provider",
        model_version="mock-v1",
        prompt_version="prompt-v1",
    )


def evidence_score() -> EvidenceScore:
    return EvidenceScore(
        evidence_id="evidence-1",
        source_quality=Decimal("0.8"),
        independence=Decimal("0.7"),
        recency=Decimal("0.9"),
        relevance=Decimal("0.8"),
        directness=Decimal("0.9"),
        extraction_confidence=Decimal("0.7"),
        total=Decimal("0.1976"),
        scoring_version="score-v1",
        scored_at=NOW,
    )


def decision_context() -> DecisionContext:
    return DecisionContext(
        context_id="context-1",
        asset_id="asset-007300",
        topic_ids=("semiconductor",),
        risk_constraints=RiskConstraints(
            maximum_position_loss_ratio=Decimal("0.47"),
            single_add_reference_range=money_range(),
            long_horizon=True,
        ),
        position=PositionRiskSnapshot(
            asset_id="asset-007300",
            portfolio_weight=Decimal("0.22"),
            unrealized_return_ratio=Decimal("0.0212"),
            loss_boundary_used=Decimal("0"),
            recurring_contribution_active=True,
            snapshot_at=NOW,
        ),
        evidence=(evidence(),),
        scores=(evidence_score(),),
        data_as_of=NOW,
        pipeline_version="pipeline-v1",
    )


def decision_result(label: SuggestionLabel = SuggestionLabel.HOLD) -> DecisionResult:
    return DecisionResult(
        decision_id="decision-1",
        context_id="context-1",
        label=label,
        strength=Decimal("0.6"),
        reasons=("模拟理由",),
        evidence_ids=("evidence-1",),
        reference_amount=money_range() if label is SuggestionLabel.SMALL_ADD else None,
        allowed_labels=(label,),
        rule_version="rule-v1",
        decided_at=NOW,
    )


def analysis_result() -> AnalysisResult:
    return AnalysisResult(
        analysis_id="analysis-1",
        context_id="context-1",
        bullish_factors=("模拟多方因素",),
        bearish_factors=("模拟空方因素",),
        uncertainties=("模拟未知因素",),
        evidence_ids=("evidence-1",),
        suggested_action=SuggestionLabel.HOLD,
        allowed_actions=(SuggestionLabel.HOLD,),
        confidence=Decimal("0.6"),
        pipeline_version="pipeline-v1",
        model_version="mock-v1",
        prompt_version="prompt-v1",
        analyzed_at=NOW,
    )


def report() -> Report:
    return Report(
        report_id="report-1",
        asset_id="asset-007300",
        topic_ids=("semiconductor",),
        decision=decision_result(),
        analysis=analysis_result(),
        source_ids=("source-1",),
        evidence_ids=("evidence-1",),
        data_as_of=NOW,
        data_is_stale=False,
        generated_at=NOW,
        pipeline_version="pipeline-v1",
        rule_version="rule-v1",
        prompt_version="prompt-v1",
        template_version="template-v1",
    )


OPENED_ON = date(2026, 4, 1)
