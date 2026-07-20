from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path

from sqlalchemy import select

from app.ai.evidence import MockAIProvider
from app.domain.enums import (
    AssetType,
    DocumentState,
    EvidenceDirection,
    SourceKind,
    TrustTier,
)
from app.domain.evidence import Evidence, EvidenceDraft, EvidenceScore
from app.domain.portfolio import Asset
from app.domain.taxonomy import Source
from app.services.evidence_extraction import EvidenceExtractionService
from app.services.evidence_scoring import (
    DeterministicEvidenceScorer,
    EvidenceScoringContext,
    EvidenceScoringService,
)
from app.services.normalization import NormalizationService
from app.services.relevance import RelevanceService
from app.services.taxonomy import TaxonomyService
from app.storage.models import EvidenceScoreRow, RawDocumentRow
from app.storage.repositories import PortfolioRepository, RawDocumentRepository
from tests.taxonomy_factories import taxonomy_configuration
from tests.test_ai_evidence import model_output
from tests.test_normalization import database, raw_document

NOW = datetime(2026, 7, 20, 12, tzinfo=UTC)


def source(kind: SourceKind, trust: TrustTier) -> Source:
    return Source(
        source_id=f"source-{kind.value.casefold()}",
        name=kind.value,
        kind=kind,
        trust_tier=trust,
        base_url="https://example.com/",
        regions=("global",),
        languages=("en",),
        adapter_name="test-adapter",
        allowed_domains=("example.com",),
    )


def evidence() -> Evidence:
    return Evidence(
        evidence_id="evidence-score-test",
        document_id="document-score-test",
        draft=EvidenceDraft(
            claim="HBM demand increased.",
            direction=EvidenceDirection.POSITIVE,
            quote="HBM demand increased.",
            topic_ids=("semiconductor.memory",),
            confidence=Decimal("0.8"),
            claim_type="demand",
            impact_horizon="SHORT",
            directness=Decimal("0.8"),
        ),
        extracted_at=NOW,
        extractor_name="mock-ai",
        model_version="mock-v1",
        prompt_version="prompt-v1",
    )


def context(
    configured_source: Source,
    *,
    published_at: datetime = NOW,
    independent_sources: int = 1,
    reprint: bool = False,
) -> EvidenceScoringContext:
    return EvidenceScoringContext(
        evidence=evidence(),
        source=configured_source,
        relevance=Decimal("0.9"),
        published_at=published_at,
        independent_source_count=independent_sources,
        same_origin_reprint=reprint,
    )


def test_source_baselines_caps_and_recency_are_deterministic() -> None:
    scorer = DeterministicEvidenceScorer()
    primary = scorer.score(context(source(SourceKind.REGULATOR, TrustTier.PRIMARY)), scored_at=NOW)
    aggregator = scorer.score(
        context(source(SourceKind.AGGREGATOR, TrustTier.SECONDARY)), scored_at=NOW
    )
    social = scorer.score(
        context(source(SourceKind.SOCIAL, TrustTier.SENTIMENT_ONLY)), scored_at=NOW
    )
    aged = scorer.score(
        context(
            source(SourceKind.REGULATOR, TrustTier.PRIMARY),
            published_at=NOW - timedelta(days=3),
        ),
        scored_at=NOW,
    )
    assert primary.total > aggregator.total > social.total
    assert aggregator.confidence_cap == Decimal("0.3500")
    assert social.confidence_cap == Decimal("0.1500")
    assert aged.recency == Decimal("0.5000")
    assert primary.component_reasons


def test_fifty_same_origin_reprints_add_no_independent_weight() -> None:
    scorer = DeterministicEvidenceScorer()
    configured = source(SourceKind.MEDIA, TrustTier.SECONDARY)
    scores = [
        scorer.score(
            context(configured, independent_sources=1, reprint=index > 0),
            scored_at=NOW,
        )
        for index in range(50)
    ]
    assert scores[0].total > 0
    assert all(item.independence == 0 and item.total == 0 for item in scores[1:])
    assert sum(item.total for item in scores) == scores[0].total


def test_extreme_total_cannot_override_a_weak_component() -> None:
    values = (
        DeterministicEvidenceScorer()
        .score(context(source(SourceKind.MEDIA, TrustTier.SECONDARY)), scored_at=NOW)
        .model_dump()
    )
    values["total"] = Decimal("1")
    try:
        EvidenceScore.model_validate(values)
    except ValueError as error:
        assert "weakest" in str(error)
    else:
        raise AssertionError("an impossible score must be rejected")


def test_scoring_service_persists_components_and_updates_state(tmp_path: Path) -> None:
    db = database(tmp_path)
    try:
        with db.session() as session:
            PortfolioRepository(session, "personal").add_asset(
                Asset(
                    asset_id="asset-007300",
                    fund_code="007300",
                    name="Test ETF Link",
                    asset_type=AssetType.ETF_LINK,
                    market="CN",
                )
            )
            TaxonomyService(session, "personal").publish(taxonomy_configuration())
            RawDocumentRepository(session, "personal").add_if_absent(
                raw_document(
                    "doc-ai",
                    "https://news.example/scored",
                    "Test Semiconductor update",
                    "Test chip demand increased while inventory remained constrained.",
                    discovered_at=NOW,
                )
            )
            NormalizationService(session, "personal").process_pending(now=NOW)
            RelevanceService(session, "personal").classify_pending(now=NOW)
            asyncio.run(
                EvidenceExtractionService(
                    session,
                    "personal",
                    MockAIProvider(model_output()),
                    model_version="mock-v1",
                ).extract_pending(now=NOW)
            )
            assert EvidenceScoringService(session, "personal").score_pending(now=NOW) == (1, 1, 0)
            score = session.scalar(select(EvidenceScoreRow))
            assert score is not None
            assert score.payload["scoring_version"] == "evidence-score-1.0.0"
            assert score.payload["component_reasons"]
            raw = session.scalar(
                select(RawDocumentRow).where(RawDocumentRow.document_id == "doc-ai")
            )
            assert raw is not None and raw.state == DocumentState.SCORED.value
    finally:
        db.dispose()
