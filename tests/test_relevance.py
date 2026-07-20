from __future__ import annotations

from datetime import UTC, datetime
from hashlib import sha256
from pathlib import Path

from sqlalchemy import func, select

from app.domain.documents import NormalizedDocument
from app.domain.enums import AssetType, DocumentState, RelevanceLabel
from app.domain.portfolio import Asset
from app.services.normalization import NormalizationService
from app.services.relevance import RelevanceService, RuleBasedRelevanceClassifier
from app.services.taxonomy import TaxonomyService
from app.storage.models import (
    AuditEventRow,
    RawDocumentRow,
    RelevanceAssessmentRow,
)
from app.storage.repositories import PortfolioRepository, RawDocumentRepository
from scripts.seed_demo_taxonomy import load_demo_configuration
from tests.taxonomy_factories import taxonomy_configuration
from tests.test_normalization import database, raw_document


def normalized(document_id: str, title: str, body: str) -> NormalizedDocument:
    now = datetime(2026, 7, 20, 12, tzinfo=UTC)
    return NormalizedDocument(
        document_id=document_id,
        source_id="source-news",
        canonical_url=f"https://news.example/{document_id}",
        title=title,
        body=body,
        original_language="en",
        detected_language="en",
        original_sha256=sha256(body.encode()).hexdigest(),
        normalized_sha256=sha256(f"{title}\n{body}".encode()).hexdigest(),
        discovered_at=now,
        normalized_at=now,
    )


def test_memory_ambiguity_and_exclusions_do_not_create_false_relevance() -> None:
    classifier = RuleBasedRelevanceClassifier()
    result = classifier.assess(
        normalized(
            "doc-memory",
            "Working memory training techniques",
            "A human memory care study with no discussion of electronics.",
        ),
        load_demo_configuration(),
        assessed_at=datetime(2026, 7, 20, 12, tzinfo=UTC),
    )
    assert result.label is RelevanceLabel.IRRELEVANT
    assert result.score == 0
    assert any("排除语境" in reason for reason in result.reasons)


def test_semiconductor_topic_entity_and_supply_chain_hits_are_explainable() -> None:
    result = RuleBasedRelevanceClassifier().assess(
        normalized(
            "doc-hbm",
            "Semiconductor HBM memory chip supply remains tight",
            "Samsung and Micron discuss DRAM and data center demand.",
        ),
        load_demo_configuration(),
        assessed_at=datetime(2026, 7, 20, 12, tzinfo=UTC),
    )
    assert result.label is RelevanceLabel.RELEVANT
    assert "semiconductor.memory" in result.topic_ids
    assert "company.micron" in result.entity_ids
    assert {hit.rule_type for hit in result.hits} == {"topic_term", "entity_term"}
    assert result.rule_version == "semiconductor-keywords-1.0.0"


def test_service_persists_decision_state_and_audited_human_label(tmp_path: Path) -> None:
    db = database(tmp_path)
    now = datetime(2026, 7, 20, 12, tzinfo=UTC)
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
                    "doc-relevant",
                    "https://news.example/relevant",
                    "Test Semiconductor update",
                    "Test chip demand increased.",
                    discovered_at=now,
                )
            )
            NormalizationService(session, "personal").process_pending(now=now)
            assert RelevanceService(session, "personal").classify_pending(now=now) == (1, 0, 0)
            assessment = session.scalar(select(RelevanceAssessmentRow))
            assert assessment is not None
            assert assessment.payload["reasons"]
            raw = session.scalar(
                select(RawDocumentRow).where(RawDocumentRow.document_id == "doc-relevant")
            )
            assert raw is not None and raw.state == DocumentState.CLASSIFIED.value

            label = RelevanceService(session, "personal").add_human_label(
                "doc-relevant",
                RelevanceLabel.RELEVANT,
                note="fixed test label",
                now=now,
            )
            assert label.document_id == "doc-relevant"
            assert session.scalar(select(func.count()).select_from(AuditEventRow)) >= 2
    finally:
        db.dispose()
