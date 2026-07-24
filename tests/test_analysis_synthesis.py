from __future__ import annotations

import asyncio
import json
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path

import httpx
import pytest
from pydantic import ValidationError
from sqlalchemy import select

from app.ai.evidence import AIProviderError
from app.ai.synthesis import (
    SYNTHESIS_PROMPT_VERSION,
    AnalysisModelOutput,
    DeepSeekAIProvider,
    MockSynthesisProvider,
    RuleSynthesisProvider,
    _bounded_payload,
)
from app.collectors.safe_http import SafeHTTPClient
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


class StaticResolver:
    async def resolve(self, hostname: str, port: int) -> tuple[str, ...]:
        assert (hostname, port) == ("api.deepseek.com", 443)
        return ("93.184.216.34",)


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


def test_large_evidence_set_is_balanced_and_bounded_before_synthesis() -> None:
    directions = (
        *(EvidenceDirection.POSITIVE for _ in range(25)),
        *(EvidenceDirection.NEGATIVE for _ in range(25)),
        *(EvidenceDirection.NEUTRAL for _ in range(12)),
    )
    value = context(tuple(directions))
    decision = DeterministicDecisionPolicy(decided_at=NOW).evaluate(value)
    large_request = AnalysisRequest(
        context=value,
        decision=decision,
        prompt_version=SYNTHESIS_PROMPT_VERSION,
        analyzed_at=NOW,
    )

    payload = _bounded_payload(large_request, max_evidence_characters=100_000)
    selected = payload["evidence"]
    selected_directions = [item["direction"] for item in selected]

    assert len(selected) == 16
    assert payload["evidence_selection"] == {
        "total_valid_evidence": 62,
        "selected_for_synthesis": 16,
        "maximum_selected": 16,
    }
    assert selected_directions.count("POSITIVE") >= 5
    assert selected_directions.count("NEGATIVE") >= 5
    assert selected_directions.count("NEUTRAL") >= 3


def test_truncated_synthesis_is_reported_without_retrying_same_limit() -> None:
    calls = 0

    async def handler(_: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(
            200,
            json={
                "model": "deepseek-v4-flash",
                "choices": [
                    {
                        "finish_reason": "length",
                        "message": {"content": '{"stance":"MIXED"', "tool_calls": None},
                    }
                ],
                "usage": {"prompt_tokens": 4000, "completion_tokens": 2400},
            },
        )

    async def scenario() -> None:
        async with SafeHTTPClient(
            resolver=StaticResolver(), transport=httpx.MockTransport(handler)
        ) as client:
            provider = DeepSeekAIProvider(
                credential="test",
                client=client,
                max_output_tokens=2_400,
            )
            with pytest.raises(AIProviderError, match="OUTPUT_TRUNCATED"):
                await provider.synthesize(request())
            assert provider.last_output_tokens == 2_400

    asyncio.run(scenario())
    assert calls == 1


def test_rule_fallback_is_explicit_and_never_expands_actions() -> None:
    result = asyncio.run(RuleSynthesisProvider().synthesize(request()))
    assert result.degraded is True
    assert result.stance == "MIXED"
    assert result.suggested_action is request().decision.label
    assert result.uncertainties
    truncated = asyncio.run(
        RuleSynthesisProvider(fallback_reason="OUTPUT_TRUNCATED").synthesize(request())
    )
    assert "输出达到长度上限" in truncated.summary


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
