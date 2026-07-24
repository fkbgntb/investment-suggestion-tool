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
    _SYSTEM_PROMPT,
    SYNTHESIS_PROMPT_VERSION,
    AnalysisModelOutput,
    DeepSeekAIProvider,
    MockSynthesisProvider,
    RuleSynthesisProvider,
    _bounded_payload,
    _parse_model_output,
    _validation_error_code,
)
from app.collectors.safe_http import SafeHTTPClient
from app.domain.analysis import CausalChain, CausalChainStep
from app.domain.contracts import AnalysisRequest
from app.domain.enums import (
    EvidenceDirection,
    SourceKind,
    SuggestionLabel,
    TrustTier,
)
from app.services.analysis_synthesis import AnalysisSynthesisService
from app.services.decision import DecisionRunService, DeterministicDecisionPolicy
from app.services.portfolio import PortfolioService
from app.storage.models import AnalysisResultRow, AnalysisRunRow, DecisionResultRow
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
        summary="需求有所增强，但库存风险仍未消除。",
        bullish_evidence_ids=("evidence-decision-1",),
        bullish_factors_zh=("需求增强可能支持价格。",),
        bearish_evidence_ids=("evidence-decision-2",),
        bearish_factors_zh=("库存风险仍未消除。",),
        unknowns=("两种影响的持续时间均未得到确认。",),
        causal_chains=(
            CausalChain(
                steps=(
                    CausalChainStep(
                        evidence_id="evidence-decision-1",
                        relation="需求可能对价格形成支撑。",
                        confidence=Decimal("0.3"),
                    ),
                ),
                conclusion="价格可能获得一定支撑。",
                confidence=Decimal("0.3"),
            ),
        ),
        invalidation_triggers=("新的独立库存披露与现有说法矛盾。",),
        suggested_action=action,
    )


def test_conflicting_evidence_is_preserved_and_confidence_is_locally_capped() -> None:
    result = asyncio.run(MockSynthesisProvider(output()).synthesize(request()))
    assert result.stance == "MIXED"
    assert result.bullish_evidence_ids == ("evidence-decision-1",)
    assert result.bearish_evidence_ids == ("evidence-decision-2",)
    assert result.confidence == Decimal("0.3600")
    assert result.suggested_action is SuggestionLabel.HOLD
    assert result.bullish_factors == ("需求增强可能支持价格。",)
    assert result.bearish_factors == ("库存风险仍未消除。",)


def test_secondary_only_directional_evidence_caps_reliability_at_035() -> None:
    value = mixed_context()
    secondary_scores = tuple(
        score.model_copy(
            update={
                "source_kind": SourceKind.AGGREGATOR,
                "trust_tier": TrustTier.SECONDARY,
                "total": Decimal("0.30"),
            }
        )
        for score in value.scores
    )
    secondary_context = value.model_copy(update={"scores": secondary_scores})
    decision = DeterministicDecisionPolicy(decided_at=NOW).evaluate(secondary_context)
    secondary_request = AnalysisRequest(
        context=secondary_context,
        decision=decision,
        prompt_version=SYNTHESIS_PROMPT_VERSION,
        analyzed_at=NOW,
    )

    result = asyncio.run(MockSynthesisProvider(output()).synthesize(secondary_request))

    assert result.confidence == Decimal("0.3500")


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
    assert payload["language"] == "Simplified Chinese for every user-facing field"
    assert "Simplified Chinese" in _SYSTEM_PROMPT


def test_directional_evidence_requires_one_chinese_factor_each() -> None:
    with pytest.raises(ValidationError, match="Chinese factor"):
        AnalysisModelOutput.model_validate(output().model_dump() | {"bullish_factors_zh": ()})


def test_user_facing_model_output_rejects_english_text() -> None:
    with pytest.raises(ValidationError, match="must be Chinese") as captured:
        AnalysisModelOutput.model_validate(
            output().model_dump() | {"summary": "English-only summary"}
        )
    assert _validation_error_code(captured.value) == "NON_CHINESE_SYNTHESIS"


def test_model_chain_confidence_is_only_clamped_downward() -> None:
    payload = output().model_dump(mode="json")
    payload["causal_chains"][0]["steps"][0]["confidence"] = "0.20"
    payload["causal_chains"][0]["confidence"] = "0.80"

    parsed = _parse_model_output(json.dumps(payload, ensure_ascii=False))

    assert parsed.causal_chains[0].confidence == Decimal("0.20")
    assert parsed.causal_chains[0].steps[0].confidence == Decimal("0.20")


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


def test_synthesis_selects_at_most_one_claim_from_each_document() -> None:
    value = mixed_context()
    duplicate = value.evidence[0].model_copy(
        update={
            "evidence_id": "duplicate-claim",
            "draft": value.evidence[0].draft.model_copy(
                update={"claim": "another claim from the same article"}
            ),
        }
    )
    duplicate_score = value.scores[0].model_copy(update={"evidence_id": "duplicate-claim"})
    duplicated = value.model_copy(
        update={
            "evidence": (*value.evidence, duplicate),
            "scores": (*value.scores, duplicate_score),
        }
    )
    decision = DeterministicDecisionPolicy(decided_at=NOW).evaluate(duplicated)
    selected = _bounded_payload(
        AnalysisRequest(
            context=duplicated,
            decision=decision,
            prompt_version=SYNTHESIS_PROMPT_VERSION,
            analyzed_at=NOW,
        )
    )["evidence"]

    assert (
        len(
            {
                item.evidence_id
                for item in duplicated.evidence
                if item.evidence_id in {entry["evidence_id"] for entry in selected}
            }
        )
        == 2
    )


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
    assert result.bullish_factors == (
        "证据抽取阶段保存了利多方向标签；由于本次未完成 AI 综合，"
        "系统不把这些标签展示为可直接采用的语义结论。",
    )
    assert "evidence-decision" not in " ".join(result.bullish_factors)
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


def test_synthesis_service_can_target_current_run_without_consuming_backlog(
    tmp_path: Path,
) -> None:
    db = database(tmp_path)
    try:
        with db.session() as session:
            portfolio = PortfolioService(session, "personal-demo")
            portfolio.create_profile(investment_profile())
            portfolio.create_asset(asset())
            portfolio.create_position(position())
            snapshot = portfolio.create_analysis_snapshot("position-007300", generated_at=NOW)
            older_context = mixed_context().model_copy(update={"context_id": "older-context"})
            current_context = mixed_context().model_copy(update={"context_id": "current-context"})
            older = DecisionRunService(session, "personal-demo").run(
                older_context,
                position_snapshot_id=snapshot.snapshot_id,
                now=NOW,
            )
            current = DecisionRunService(session, "personal-demo").run(
                current_context,
                position_snapshot_id=snapshot.snapshot_id,
                now=NOW,
            )
            older_row = session.scalar(
                select(DecisionResultRow).where(DecisionResultRow.decision_id == older.decision_id)
            )
            current_row = session.scalar(
                select(DecisionResultRow).where(
                    DecisionResultRow.decision_id == current.decision_id
                )
            )
            assert older_row is not None and current_row is not None
            service = AnalysisSynthesisService(
                session,
                "personal-demo",
                MockSynthesisProvider(output()),
                model_version="mock-v1",
            )

            assert asyncio.run(
                service.synthesize_pending(
                    now=NOW,
                    limit=1,
                    analysis_run_id=current_row.analysis_run_id,
                )
            ) == (1, 0, 0)

            analyzed_ids = set(session.scalars(select(AnalysisResultRow.analysis_run_id)).all())
            assert analyzed_ids == {current_row.analysis_run_id}
            assert older_row.analysis_run_id not in analyzed_ids
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
