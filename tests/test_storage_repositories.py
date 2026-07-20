from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path

import pytest
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError

from app.domain.base import IdempotencyKey
from app.domain.collection import FetchFailure
from app.domain.documents import ExternalDocumentContent, RawDocument, RawDocumentControl
from app.domain.enums import AssetType, DocumentState, FetchErrorCode, SourceKind, TrustTier
from app.domain.portfolio import Asset, Position
from app.domain.state_machine import evaluate_document_transition
from app.domain.taxonomy import Source
from app.storage.database import Database
from app.storage.migrations import upgrade_database
from app.storage.models import AuditEventRow, CrawlRunRow, RawDocumentRow, SourceRow, WorkspaceRow
from app.storage.repositories import (
    AuditDetailRejected,
    AuditRepository,
    ConcurrentStateChange,
    CrawlRunInput,
    CrawlRunRepository,
    IdempotencyConflict,
    PortfolioRepository,
    RawDocumentRepository,
    SourceRepository,
    WorkspaceRepository,
)
from app.storage.retention import purge_expired_raw_bodies
from tests.domain_factories import OPENED_ON, investment_profile, money

HASH = "b" * 64
NOW = datetime(2026, 7, 19, 12, 0, tzinfo=UTC)


def database(tmp_path: Path) -> Database:
    url = f"sqlite:///{(tmp_path / 'storage.sqlite3').as_posix()}"
    upgrade_database(url)
    return Database(url)


def source(source_id: str = "source-sec") -> Source:
    return Source(
        source_id=source_id,
        name="SEC",
        kind=SourceKind.REGULATOR,
        trust_tier=TrustTier.PRIMARY,
        base_url="https://www.sec.gov/",
        regions=("US",),
        languages=("en",),
        adapter_name="sec-filings",
    )


def document(document_id: str = "document-1", *, fetched_at: datetime = NOW) -> RawDocument:
    return RawDocument(
        external=ExternalDocumentContent(
            source_url="https://www.sec.gov/example",
            title="Example filing",
            body="Untrusted source text",
            published_at=NOW,
            language="en",
        ),
        control=RawDocumentControl(
            document_id=document_id,
            source_id="source-sec",
            state=DocumentState.FETCHED,
            state_version=1,
            content_sha256=HASH,
            idempotency=IdempotencyKey(scope="raw-document", key="example", payload_sha256=HASH),
            discovered_at=fetched_at,
            fetched_at=fetched_at,
        ),
    )


def test_repository_deduplicates_and_retention_preserves_metadata(tmp_path: Path) -> None:
    db = database(tmp_path)
    try:
        with db.session() as session:
            WorkspaceRepository(session).create("personal", "Personal")
            SourceRepository(session, "personal").add(source())
            repository = RawDocumentRepository(session, "personal")
            first, created = repository.add_if_absent(document(fetched_at=NOW - timedelta(days=91)))
            duplicate, duplicate_created = repository.add_if_absent(document("document-2"))

            assert created is True
            assert duplicate_created is False
            assert duplicate.document_id == first.document_id
            assert (
                purge_expired_raw_bodies(
                    session,
                    workspace_id="personal",
                    now=NOW,
                    retention_days=90,
                )
                == 1
            )

        with db.session() as session:
            stored = session.get(RawDocumentRow, "document-1")
            assert stored is not None
            assert stored.raw_body is None
            assert stored.content_hash == HASH
            assert stored.source_url == "https://www.sec.gov/example"
            assert stored.body_purged_at == NOW
    finally:
        db.dispose()


def test_database_blocks_cross_workspace_parent_reference(tmp_path: Path) -> None:
    db = database(tmp_path)
    try:
        with pytest.raises(IntegrityError), db.session() as session:
            workspaces = WorkspaceRepository(session)
            workspaces.create("workspace-a", "A")
            workspaces.create("workspace-b", "B")
            SourceRepository(session, "workspace-b").add(source())
            session.add(
                RawDocumentRow(
                    document_id="cross-workspace",
                    workspace_id="workspace-a",
                    source_id="source-sec",
                    source_url="https://www.sec.gov/cross",
                    title="Cross workspace",
                    raw_body="blocked",
                    content_hash="c" * 64,
                    state=DocumentState.FETCHED.value,
                    state_version=1,
                    fetched_at=NOW,
                )
            )
    finally:
        db.dispose()


def test_portfolio_and_crawl_job_repositories_persist_versioned_snapshots(tmp_path: Path) -> None:
    db = database(tmp_path)
    try:
        with db.session() as session:
            WorkspaceRepository(session).create("personal", "Personal")
            SourceRepository(session, "personal").add(source())
            portfolio = PortfolioRepository(session, "personal")
            profile = investment_profile()
            asset = Asset(
                asset_id="asset-007300",
                fund_code="007300",
                name="Semiconductor ETF Link A",
                asset_type=AssetType.ETF_LINK,
                market="CN",
                tracking_asset_code="512480",
            )
            position = Position(
                position_id="position-007300",
                profile_id=profile.profile_id,
                asset_id=asset.asset_id,
                units=Decimal("157.89"),
                cost_basis=money("640"),
                current_value=money("653.59"),
                average_cost_per_unit=Decimal("4.0535"),
                opened_on=OPENED_ON,
                latest_purchase_on=OPENED_ON.replace(month=7),
                recurring_contribution=money("50"),
                snapshot_at=NOW,
            )
            portfolio.add_profile(profile)
            portfolio.add_asset(asset)
            stored_position = portfolio.add_position(position)
            assert stored_position.payload["current_value"]["amount"] == "653.59"

            crawl = CrawlRunInput(
                crawl_run_id="crawl-1",
                workspace_id="personal",
                source_id="source-sec",
                idempotency_key="sec:2026-07-19T12",
                status="PENDING",
                scheduled_at=NOW,
                payload={"cursor": "public-filing-cursor"},
            )
            first, first_created = CrawlRunRepository(session).add_if_absent(crawl)
            duplicate, duplicate_created = CrawlRunRepository(session).add_if_absent(crawl)
            assert first_created is True
            assert duplicate_created is False
            assert duplicate.crawl_run_id == first.crawl_run_id
            conflicting = CrawlRunInput(
                crawl_run_id="crawl-2",
                workspace_id="personal",
                source_id="source-sec",
                idempotency_key="sec:2026-07-19T12",
                status="PENDING",
                scheduled_at=NOW,
                payload={"cursor": "different-cursor"},
            )
            with pytest.raises(IdempotencyConflict):
                CrawlRunRepository(session).add_if_absent(conflicting)
    finally:
        db.dispose()


def test_document_transition_uses_optimistic_version_guard(tmp_path: Path) -> None:
    db = database(tmp_path)
    try:
        with db.session() as session:
            WorkspaceRepository(session).create("personal", "Personal")
            SourceRepository(session, "personal").add(source())
            repository = RawDocumentRepository(session, "personal")
            repository.add_if_absent(document())
            transition = evaluate_document_transition(
                document_id="document-1",
                current=DocumentState.FETCHED,
                requested=DocumentState.NORMALIZED,
                state_version=1,
                occurred_at=NOW + timedelta(minutes=1),
            )
            assert repository.apply_transition(transition) is True
            with pytest.raises(ConcurrentStateChange):
                repository.apply_transition(transition)

            noop = evaluate_document_transition(
                document_id="document-1",
                current=DocumentState.NORMALIZED,
                requested=DocumentState.NORMALIZED,
                state_version=2,
                occurred_at=NOW + timedelta(minutes=2),
            )
            assert repository.apply_transition(noop) is False
    finally:
        db.dispose()


def test_crawl_failure_persists_only_sanitized_reason(tmp_path: Path) -> None:
    db = database(tmp_path)
    try:
        with db.session() as session:
            WorkspaceRepository(session).create("personal", "Personal")
            SourceRepository(session, "personal").add(source())
            repository = CrawlRunRepository(session)
            repository.add_if_absent(
                CrawlRunInput(
                    crawl_run_id="crawl-failed",
                    workspace_id="personal",
                    source_id="source-sec",
                    idempotency_key="sec:failed",
                    status="RUNNING",
                    scheduled_at=NOW,
                    payload={"cursor": "public-cursor"},
                )
            )
            assert repository.mark_fetch_failure(
                workspace_id="personal",
                crawl_run_id="crawl-failed",
                failure=FetchFailure(
                    source_id="source-sec",
                    error_code=FetchErrorCode.TIMEOUT,
                    retryable=True,
                    occurred_at=NOW,
                ),
            )

            row = session.scalar(
                select(CrawlRunRow).where(CrawlRunRow.crawl_run_id == "crawl-failed")
            )
            assert row is not None
            assert row.status == "RETRYABLE_FAILED"
            assert row.payload["failure"] == {"error_code": "TIMEOUT", "retryable": True}
            serialized = str(row.payload)
            assert "http" not in serialized
            assert "127.0.0.1" not in serialized
    finally:
        db.dispose()


def test_workspace_delete_cascades_sensitive_records(tmp_path: Path) -> None:
    db = database(tmp_path)
    try:
        with db.session() as session:
            WorkspaceRepository(session).create("personal", "Personal")
            SourceRepository(session, "personal").add(source())
            RawDocumentRepository(session, "personal").add_if_absent(document())
            AuditRepository(session, "personal").record(
                event_type="created",
                actor="test",
                target_type="document",
                target_id="document-1",
                outcome="completed",
            )

        with db.session() as session:
            assert WorkspaceRepository(session).delete("personal") is True

        with db.session() as session:
            assert session.scalar(select(func.count()).select_from(WorkspaceRow)) == 0
            assert session.scalar(select(func.count()).select_from(SourceRow)) == 0
            assert session.scalar(select(func.count()).select_from(RawDocumentRow)) == 0
            assert session.scalar(select(func.count()).select_from(AuditEventRow)) == 0
    finally:
        db.dispose()


def test_audit_rejects_sensitive_or_oversized_details(tmp_path: Path) -> None:
    db = database(tmp_path)
    try:
        with db.session() as session:
            WorkspaceRepository(session).create("personal", "Personal")
            audit = AuditRepository(session, "personal")
            with pytest.raises(AuditDetailRejected, match="sensitive"):
                audit.record(
                    event_type="unsafe",
                    actor="test",
                    target_type="request",
                    target_id="1",
                    outcome="rejected",
                    details={"api-key-value": "must-not-be-logged"},
                )
            with pytest.raises(AuditDetailRejected, match="20 KB"):
                audit.record(
                    event_type="oversized",
                    actor="test",
                    target_type="request",
                    target_id="2",
                    outcome="rejected",
                    details={"message": "界" * 7000},
                )
    finally:
        db.dispose()
