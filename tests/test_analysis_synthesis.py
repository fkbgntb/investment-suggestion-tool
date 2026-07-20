from __future__ import annotations

import asyncio
import json
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path

import pytest
from pydantic import ValidationError
from sqlalchemy import select

from app.ai.synthesis import (
    SYNTHESIS_PROMPT_VERSION,
    AnalysisModelOutput,
    MockSynthesisProvider,
    RuleSynthesisProvider,
    _bounded_payload,
)
from app.domain.analysis import CausalChain, CausalChainStep
from app.domain.contracts import AnalysisRequest
from app.domain.enums import EvidenceDirection, SuggestionLabel
from app.services.analysis_synthesis import AnalysisSynthesisService
from app.services.decision import DecisionRunService, DeterministicDecisionPolicy
from app.services.portfolio import PortfolioService
from app.storage.models import AnalysisResultRow, AnalysisRunRow
from tests.domain_factories import investment_profile
from tests.test_decision_policy import context
from tests.test_portfolio_service import asset, database, position

NOW = datetime(2026, 7, 20, 12, tzinfo=UTC)


def mixed_context():
    return context((EvidenceDirection.POSITIVE, EvidenceDirection.NEGATIVE))


def request():
    value = mixed_context()
    decision = DeterministicDecisionPolicy(decided_at=NOW).evaluate(value)
    return AnalysisRequest(
        context=value,
        decision=decision,
        prompt_version=SYNTHESIS_PROMPT_VERSION,
        analyzed_at=NOW,
    )


def output(*, action: SuggestionLabel = SuggestionLabel.HOLD) -> AnalysisModelOutput:
    return AnalysisModelOutput(
        stance="MIXED",
        confidence=Decimal("0.9"),
        summary="Demand is stronger while inventory risk remains unresolved.",
        bullish_evidence_ids=("evidence-decision-1",),
        bearish_evidence_ids=("evidence-decision-2",),
        unknowns=("The duration of either effect is not confirmed.",),
        causal_chains=(
            CausalChain(
                steps=(
                    CausalChainStep(
                        evidence_id="evidence-decision-1",
                        relation="demand may support pricing",
                        confidence=Decimal("0.3"),
                    ),
                ),
                conclusion="pricing support is possible",
                confidence=Decimal("0.3"),
            ),
        ),
        invalidation_triggers=("A new independent inventory disclosure contradicts the claim.",),
        suggested_action=action,
    )


def test_conflicting_evidence_is_preserved_and_confidence_is_locally_capped() -> None:
    result = asyncio.run(MockSynthesisProvider(output()).synthesize(request()))
    assert result.stance == "MIXED"
    assert result.bullish_evidence_ids == ("evidence-decision-1",)
    assert result.bearish_evidence_ids == ("evidence-decision-2",)
    assert result.confidence == Decimal("0.3600")
    assert result.suggested_action is SuggestionLabel.HOLD


def test_unknown_reference_and_out_of_bounds_action_are_rejected() -> None:
    unknown = output().model_copy(update={"bullish_evidence_ids": ("invented-evidence",)})
    with pytest.raises(ValueError, match="unknown evidence"):
        asyncio.run(MockSynthesisProvider(unknown).synthesize(request()))
    with pytest.raises(ValueError, match="allowed set"):
        asyncio.run(
            MockSynthesisProvider(output(action=SuggestionLabel.REDUCE)).synthesize(request())
        )


def test_request_rejects_decision_from_another_context() -> None:
    value = request()
    wrong = value.decision.model_copy(update={"context_id": "other-context"})
    with pytest.raises(ValidationError, match="must match"):
        AnalysisRequest(
            context=value.context,
            decision=wrong,
            prompt_version=value.prompt_version,
            analyzed_at=NOW,
        )


def test_provider_payload_excludes_exact_money_and_identity() -> None:
    payload = _bounded_payload(request())
    serialized = str({key: value for key, value in payload.items() if key != "output_schema"})
    for forbidden in ("profile-demo", "100", "200", "CNY", "position-007300"):
        assert forbidden not in serialized
    assert "portfolio_weight" in serialized
    assert "allowed_actions" in serialized


def test_provider_evidence_payload_has_a_hard_character_budget() -> None:
    payload = _bounded_payload(request(), max_evidence_characters=700)
    encoded_entries = sum(
        len(json.dumps(item, ensure_ascii=False, separators=(",", ":")))
        for item in payload["evidence"]
    )
    assert encoded_entries <= 700


def test_rule_fallback_is_explicit_and_never_expands_actions() -> None:
    result = asyncio.run(RuleSynthesisProvider().synthesize(request()))
    assert result.degraded is True
    assert result.stance == "MIXED"
    assert result.suggested_action is request().decision.label
    assert result.uncertainties


def test_synthesis_service_persists_usage_and_is_idempotent(tmp_path: Path) -> None:
    db = database(tmp_path)
    try:
        with db.session() as session:
            portfolio = PortfolioService(session, "personal-demo")
            portfolio.create_profile(investment_profile())
            portfolio.create_asset(asset())
            portfolio.create_position(position())
            snapshot = portfolio.create_analysis_snapshot("position-007300", generated_at=NOW)
            value = mixed_context()
            DecisionRunService(session, "personal-demo").run(
                value, position_snapshot_id=snapshot.snapshot_id, now=NOW
            )
            service = AnalysisSynthesisService(
                session,
                "personal-demo",
                MockSynthesisProvider(output()),
                model_version="mock-v1",
            )
            assert asyncio.run(service.synthesize_pending(now=NOW)) == (1, 0, 0)
            assert asyncio.run(service.synthesize_pending(now=NOW)) == (0, 0, 0)
            row = session.scalar(select(AnalysisResultRow))
            run = session.scalar(select(AnalysisRunRow))
            assert row is not None and run is not None
            assert row.input_tokens == 100 and row.output_tokens == 80
            assert row.payload["suggested_action"] == SuggestionLabel.HOLD.value
            assert run.payload["status"] == "ANALYZED"
    finally:
        db.dispose()


def test_daily_limit_degrades_instead_of_calling_model(tmp_path: Path) -> None:
    db = database(tmp_path)
    try:
        with db.session() as session:
            portfolio = PortfolioService(session, "personal-demo")
            portfolio.create_profile(investment_profile())
            portfolio.create_asset(asset())
            portfolio.create_position(position())
            snapshot = portfolio.create_analysis_snapshot("position-007300", generated_at=NOW)
            DecisionRunService(session, "personal-demo").run(
                mixed_context(), position_snapshot_id=snapshot.snapshot_id, now=NOW
            )
            provider = MockSynthesisProvider(output())
            provider.provider_name = "deepseek"
            service = AnalysisSynthesisService(
                session,
                "personal-demo",
                provider,
                model_version="mock-v1",
                max_calls_per_day=0,
            )
            assert asyncio.run(service.synthesize_pending(now=NOW)) == (1, 1, 1)
            row = session.scalar(select(AnalysisResultRow))
            assert row is not None
            assert row.provider_name == "rule-synthesis"
            assert row.payload["provider_name"] == "rule-synthesis"
            assert row.error_code == "DAILY_BUDGET_REACHED"
    finally:
        db.dispose()
