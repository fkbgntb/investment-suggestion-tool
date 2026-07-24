from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path

from sqlalchemy import func, select

from app.collectors.registry import AdapterRegistry
from app.config import Settings
from app.domain.enums import (
    DocumentState,
    EvidenceDirection,
    SourceKind,
    SuggestionLabel,
    TrustTier,
)
from app.domain.evidence import Evidence, EvidenceDraft, EvidenceScore
from app.domain.taxonomy import Source
from app.services.portfolio import PortfolioService
from app.services.report_triggers import ScheduledReportTriggerService
from app.services.sources import SourceService
from app.storage.models import (
    EvidenceItemRow,
    EvidenceScoreRow,
    RawDocumentRow,
    ReportRow,
    ScheduledTaskRow,
)
from app.storage.repositories import TaskQueueRepository
from tests.domain_factories import investment_profile
from tests.test_portfolio_service import asset, database, position

NOW = datetime(2026, 7, 20, 12, tzinfo=UTC)


def settings(tmp_path: Path) -> Settings:
    return Settings(
        _env_file=None,
        environment="test",
        data_dir=tmp_path,
        database_url=f"sqlite:///{(tmp_path / 'portfolio.sqlite3').as_posix()}",
        portfolio_workspace_id="personal-demo",
        portfolio_reference_value="3000",
    )


def configured_source(kind: SourceKind, trust: TrustTier) -> Source:
    return Source(
        source_id=f"source-{kind.value.casefold()}",
        name=f"Test {kind.value}",
        kind=kind,
        trust_tier=trust,
        base_url="https://example.com/",
        regions=("global",),
        languages=("en",),
        adapter_name="test-adapter",
        allowed_domains=("example.com",),
    )


def seed(
    session,
    *,
    kind: SourceKind,
    trust: TrustTier,
    published_at: datetime = NOW,
) -> None:
    portfolio = PortfolioService(session, "personal-demo")
    portfolio.create_profile(investment_profile())
    portfolio.create_asset(asset())
    portfolio.create_position(position())
    source = configured_source(kind, trust)
    SourceService(
        session,
        "personal-demo",
        AdapterRegistry(("test-adapter",)),
    ).create(source)
    for index in range(2):
        document_id = f"document-{kind.value.casefold()}-{index}"
        evidence_id = f"evidence-{kind.value.casefold()}-{index}"
        session.add(
            RawDocumentRow(
                document_id=document_id,
                workspace_id="personal-demo",
                source_id=source.source_id,
                source_url=f"https://example.com/{document_id}",
                title=f"Semiconductor evidence {index}",
                raw_body=f"Official semiconductor demand disclosure {index}.",
                content_hash=f"{index + 1:064x}",
                schema_version="1.0",
                state=DocumentState.SCORED.value,
                state_version=1,
                published_at=published_at,
                fetched_at=published_at,
                metadata_payload={},
            )
        )
        session.flush()
        evidence = Evidence(
            evidence_id=evidence_id,
            document_id=document_id,
            draft=EvidenceDraft(
                claim=f"Semiconductor demand increased {index}.",
                direction=EvidenceDirection.POSITIVE,
                topic_ids=("semiconductor",),
                confidence=Decimal("0.9"),
                claim_type="official_fact",
                impact_horizon="SHORT",
                directness=Decimal("0.9"),
            ),
            extracted_at=published_at,
            extractor_name="rules",
            model_version="rules-1",
            prompt_version="prompt-1",
        )
        score = EvidenceScore(
            evidence_id=evidence_id,
            source_quality=Decimal("0.95") if trust is TrustTier.PRIMARY else Decimal("0.45"),
            independence=Decimal("0.75"),
            recency=Decimal("1"),
            relevance=Decimal("0.9"),
            directness=Decimal("0.9"),
            extraction_confidence=Decimal("0.9"),
            total=Decimal("0.20"),
            source_kind=kind,
            trust_tier=trust,
            independent_source_count=2,
            confidence_cap=Decimal("0.35") if kind is SourceKind.AGGREGATOR else None,
            scoring_version="score-1",
            scored_at=published_at,
        )
        session.add(
            EvidenceItemRow(
                evidence_id=evidence_id,
                workspace_id="personal-demo",
                document_id=document_id,
                cluster_id=None,
                schema_version=evidence.schema_version,
                payload=evidence.model_dump(mode="json"),
            )
        )
        session.flush()
        session.add(
            EvidenceScoreRow(
                score_id=f"score-{kind.value.casefold()}-{index}",
                workspace_id="personal-demo",
                evidence_id=evidence_id,
                scoring_version=score.scoring_version,
                schema_version=score.schema_version,
                payload=score.model_dump(mode="json"),
            )
        )
    session.flush()


def enqueue(session, task_id: str, task_type: str, *, not_before: datetime = NOW) -> None:
    TaskQueueRepository(session, "personal-demo").enqueue(
        scope=f"test:{task_type}",
        key=task_id,
        payload_sha256=f"{len(task_id):064x}",
        task_id=task_id,
        task_type=task_type,
        payload={"summary_date": not_before.date().isoformat()},
        not_before=not_before,
    )


def test_new_primary_evidence_generates_report_and_persists_real_ids(tmp_path: Path) -> None:
    db = database(tmp_path)
    try:
        with db.session() as session:
            seed(session, kind=SourceKind.REGULATOR, trust=TrustTier.PRIMARY)
            enqueue(session, "task-primary", "process-new-documents")
            result = asyncio.run(
                ScheduledReportTriggerService(
                    session, "personal-demo", settings(tmp_path)
                ).consume_due(now=NOW)
            )
            assert result.generated == 1
            report = session.scalar(select(ReportRow))
            task = session.get(ScheduledTaskRow, "task-primary")
            assert report is not None and task is not None
            assert set(report.input_snapshot["evidence_ids"]) == {
                "evidence-regulator-0",
                "evidence-regulator-1",
            }
            assert task.status == "SUCCEEDED"
            assert task.payload["result"]["status"] == "GENERATED"
            assert task.payload["result"]["report_id"] == report.report_id
    finally:
        db.dispose()


def test_aggregator_daily_report_is_observation_and_duplicate_is_skipped(
    tmp_path: Path,
) -> None:
    db = database(tmp_path)
    try:
        with db.session() as session:
            seed(session, kind=SourceKind.AGGREGATOR, trust=TrustTier.SECONDARY)
            enqueue(session, "task-daily-first", "daily-summary")
            first = asyncio.run(
                ScheduledReportTriggerService(
                    session, "personal-demo", settings(tmp_path)
                ).consume_due(now=NOW)
            )
            assert first.generated == 1
            report = session.scalar(select(ReportRow))
            assert report is not None
            assert report.payload["decision"]["label"] == SuggestionLabel.OBSERVE.value
            assert (
                SuggestionLabel.SMALL_ADD.value not in report.payload["decision"]["allowed_labels"]
            )
            assert SuggestionLabel.REDUCE.value not in report.payload["decision"]["allowed_labels"]

            enqueue(session, "task-daily-repeat", "daily-summary", not_before=NOW)
            repeated = asyncio.run(
                ScheduledReportTriggerService(
                    session, "personal-demo", settings(tmp_path)
                ).consume_due(now=NOW + timedelta(minutes=1))
            )
            assert repeated.generated == 0 and repeated.skipped == 1
            assert session.scalar(select(func.count()).select_from(ReportRow)) == 1
            repeat_task = session.get(ScheduledTaskRow, "task-daily-repeat")
            assert repeat_task is not None
            assert repeat_task.payload["result"]["status"] == "SKIPPED_NO_NEW_EVIDENCE"
    finally:
        db.dispose()


def test_aggregator_processing_waits_for_daily_and_expired_evidence_is_rejected(
    tmp_path: Path,
) -> None:
    db = database(tmp_path)
    try:
        with db.session() as session:
            seed(
                session,
                kind=SourceKind.AGGREGATOR,
                trust=TrustTier.SECONDARY,
                published_at=NOW - timedelta(days=13),
            )
            enqueue(session, "task-old-processing", "process-new-documents")
            enqueue(session, "task-old-daily", "daily-summary")
            result = asyncio.run(
                ScheduledReportTriggerService(
                    session, "personal-demo", settings(tmp_path)
                ).consume_due(now=NOW)
            )
            assert result.generated == 0 and result.skipped == 2
            outcomes = {
                row.task_id: row.payload["result"]["status"]
                for row in session.scalars(select(ScheduledTaskRow)).all()
            }
            assert outcomes == {
                "task-old-daily": "SKIPPED_DATA_EXPIRED",
                "task-old-processing": "SKIPPED_DATA_EXPIRED",
            }
    finally:
        db.dispose()
