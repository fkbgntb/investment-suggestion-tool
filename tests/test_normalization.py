from __future__ import annotations

from datetime import UTC, datetime, timedelta
from hashlib import sha256
from pathlib import Path

from sqlalchemy import func, select

from app.domain.base import IdempotencyKey
from app.domain.documents import ExternalDocumentContent, RawDocument, RawDocumentControl
from app.domain.enums import DocumentState, SourceKind, TrustTier
from app.domain.taxonomy import Source
from app.services.normalization import (
    HeuristicLanguageDetector,
    NormalizationService,
    canonicalize_url,
    normalize_text,
)
from app.storage.database import Database
from app.storage.migrations import upgrade_database
from app.storage.models import EventClusterRow, NormalizedDocumentRow, RawDocumentRow
from app.storage.repositories import RawDocumentRepository, SourceRepository, WorkspaceRepository


def raw_document(
    document_id: str,
    url: str,
    title: str,
    body: str,
    *,
    discovered_at: datetime,
    source_id: str = "source-news",
) -> RawDocument:
    digest = sha256(f"{document_id}:{body}".encode()).hexdigest()
    return RawDocument(
        external=ExternalDocumentContent(
            source_url=url,
            title=title,
            body=body,
            language="en",
            metadata={"summary": title},
        ),
        control=RawDocumentControl(
            document_id=document_id,
            source_id=source_id,
            state=DocumentState.FETCHED,
            state_version=1,
            content_sha256=digest,
            idempotency=IdempotencyKey(
                scope="test-normalization",
                key=document_id,
                payload_sha256=digest,
            ),
            discovered_at=discovered_at,
            fetched_at=discovered_at,
        ),
    )


def database(tmp_path: Path) -> Database:
    url = f"sqlite:///{(tmp_path / 'normalization.sqlite3').as_posix()}"
    upgrade_database(url)
    db = Database(url)
    with db.session() as session:
        WorkspaceRepository(session).create("personal", "Personal")
        SourceRepository(session, "personal").add(
            Source(
                source_id="source-news",
                name="News",
                kind=SourceKind.MEDIA,
                trust_tier=TrustTier.SECONDARY,
                base_url="https://news.example/",
                regions=("global",),
                languages=("multi",),
                adapter_name="mock-rss",
                allowed_domains=("news.example",),
            )
        )
    return db


def test_html_and_unicode_are_deterministically_sanitized() -> None:
    value = (
        "<style>.x{display:none}</style><nav>menu</nav><h1>Ｔｅｓｔ</h1>"
        "<script>alert(1)</script><div hidden>ignore me</div>"
        "<input hidden><p>安全\u200b text</p>"
    )
    cleaned, flags = normalize_text(value, maximum=1000)
    assert cleaned == "Test 安全 text"
    assert "alert" not in cleaned
    assert "menu" not in cleaned
    assert "ignore" not in cleaned
    assert "removed_script" in flags
    assert "removed_hidden_text" in flags
    assert "removed_control_characters" in flags


def test_tracking_parameters_are_removed_and_query_is_sorted() -> None:
    assert (
        canonicalize_url("HTTPS://News.Example:443/story?utm_source=x&b=2&a=1&fbclid=y#section")
        == "https://news.example/story?a=1&b=2"
    )


def test_language_detection_is_deterministic_and_replaceable() -> None:
    detector = HeuristicLanguageDetector()
    assert detector.detect("存储芯片供应仍然紧张", "und") == "zh"
    assert detector.detect("Memory chip supply remains tight", "und") == "en"
    assert detector.detect("12345", "fr-FR") == "fr"


def test_exact_duplicates_cluster_reprints_and_quarantine_empty_html(tmp_path: Path) -> None:
    db = database(tmp_path)
    now = datetime(2026, 7, 20, 12, tzinfo=UTC)
    try:
        with db.session() as session:
            repository = RawDocumentRepository(session, "personal")
            repository.add_if_absent(
                raw_document(
                    "doc-1",
                    "https://news.example/story?id=1&utm_source=feed",
                    "Memory supply remains tight",
                    "<article>Suppliers report constrained inventory.</article>",
                    discovered_at=now - timedelta(hours=2),
                )
            )
            repository.add_if_absent(
                raw_document(
                    "doc-2",
                    "https://news.example/story?utm_medium=rss&id=1",
                    "Memory supply remains tight",
                    "Suppliers report constrained inventory with an update.",
                    discovered_at=now - timedelta(hours=1),
                )
            )
            repository.add_if_absent(
                raw_document(
                    "doc-3",
                    "https://other.example/reprint",
                    "Memory supply remains tight",
                    "Suppliers reported constrained inventory across the sector.",
                    discovered_at=now,
                )
            )
            repository.add_if_absent(
                raw_document(
                    "doc-empty",
                    "https://news.example/empty",
                    "Empty",
                    "<script>only executable content</script>",
                    discovered_at=now,
                )
            )
            processed, duplicates, quarantined = NormalizationService(
                session, "personal"
            ).process_pending(now=now)
            assert (processed, duplicates, quarantined) == (3, 1, 1)
            normalized = session.scalars(
                select(NormalizedDocumentRow).order_by(NormalizedDocumentRow.document_id)
            ).all()
            assert len(normalized) == 3
            assert normalized[1].duplicate_of_document_id == "doc-1"
            assert all("<script" not in row.normalized_body for row in normalized)
            clusters = session.scalars(select(EventClusterRow)).all()
            assert len(clusters) == 1
            assert set(clusters[0].payload["document_ids"]) == {"doc-1", "doc-2", "doc-3"}
            empty = session.scalar(
                select(RawDocumentRow).where(RawDocumentRow.document_id == "doc-empty")
            )
            assert empty is not None
            assert empty.state == DocumentState.QUARANTINED.value
            assert session.scalar(select(func.count()).select_from(NormalizedDocumentRow)) == 3
            assert NormalizationService(session, "personal").process_pending(now=now) == (0, 0, 0)
    finally:
        db.dispose()
