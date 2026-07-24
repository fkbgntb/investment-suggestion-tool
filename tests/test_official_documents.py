from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from io import BytesIO
from pathlib import Path

import httpx
from pypdf import PdfWriter

from app.collectors.official_documents import (
    OfficialDocumentParser,
    OfficialDocumentRejected,
    OfficialDocumentSpec,
)
from app.collectors.registry import AdapterRegistry
from app.collectors.safe_http import SafeHTTPClient, SafeHTTPResponse
from app.domain.enums import ContentType, SourceKind, TrustTier
from app.domain.taxonomy import Source
from app.services.official_document_collection import OfficialDocumentCollectionService
from app.services.sources import SourceService
from app.storage.database import Database
from app.storage.migrations import upgrade_database
from app.storage.models import RawDocumentRow
from scripts.configure_official_sources import load_sources

NOW = datetime(2026, 7, 24, 8, tzinfo=UTC)
PUBLIC_IP = "93.184.216.34"


class StaticResolver:
    async def resolve(self, hostname: str, port: int) -> tuple[str, ...]:
        del hostname, port
        return (PUBLIC_IP,)


def pdf_bytes(*, title: str = "Official") -> bytes:
    buffer = BytesIO()
    writer = PdfWriter()
    writer.add_blank_page(width=200, height=200)
    writer.add_metadata({"/Title": title})
    writer.write(buffer)
    return buffer.getvalue()


def source() -> Source:
    return Source(
        source_id="official-test",
        name="Official test",
        kind=SourceKind.REGULATOR,
        trust_tier=TrustTier.PRIMARY,
        base_url="https://official.example.com/document.pdf",
        regions=("cn",),
        languages=("zh",),
        adapter_name="official-document",
        allowed_domains=("official.example.com",),
    )


def spec() -> OfficialDocumentSpec:
    return OfficialDocumentSpec(
        document_key="test-document",
        source_id="official-test",
        url="https://official.example.com/document.pdf",
        title="Official document",
        publisher="Official publisher",
        language="zh",
        content_type=ContentType.ANNOUNCEMENT,
        published_at=NOW,
    )


def test_pdf_parser_rejects_invalid_active_and_oversized_documents() -> None:
    parser = OfficialDocumentParser(max_pages=1)
    response = SafeHTTPResponse(
        source_id="official-test",
        final_url="https://official.example.com/document.pdf",
        status_code=200,
        content_type="application/pdf",
        body=b"not-a-pdf",
    )
    try:
        parser.parse(response, fallback_title="Fallback")
        raise AssertionError("invalid PDF was accepted")
    except OfficialDocumentRejected:
        pass

    response = SafeHTTPResponse(
        source_id="official-test",
        final_url="https://official.example.com/document.pdf",
        status_code=200,
        content_type="application/pdf",
        body=pdf_bytes() + b"/JavaScript",
    )
    try:
        parser.parse(response, fallback_title="Fallback")
        raise AssertionError("active PDF was accepted")
    except OfficialDocumentRejected:
        pass


def test_reviewed_source_catalog_has_explicit_roles_and_intervals() -> None:
    sources = load_sources()

    assert {item.source_id for item in sources} == {
        "cninfo-007300-product",
        "csi-h30184-factsheet",
        "csi-h30184-methodology",
        "micron-ir-news",
        "sse-512480-product",
        "sse-512480-split",
    }
    assert all(item.trust_tier is TrustTier.PRIMARY for item in sources)
    assert all(item.adapter_name == "official-document" for item in sources)
    assert {item.crawl_interval_hours for item in sources} <= {3, 24, 168}


def test_official_pdf_update_and_unchanged_content_are_idempotent(tmp_path: Path) -> None:
    responses = [pdf_bytes(title="v1"), pdf_bytes(title="v1"), pdf_bytes(title="v2")]

    def respond(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            headers={"Content-Type": "application/pdf"},
            content=responses.pop(0),
            request=request,
        )

    async def scenario() -> None:
        database_url = f"sqlite:///{(tmp_path / 'official.sqlite3').as_posix()}"
        upgrade_database(database_url)
        database = Database(database_url)
        try:
            async with SafeHTTPClient(
                resolver=StaticResolver(),
                transport=httpx.MockTransport(respond),
            ) as client:
                with database.session() as session:
                    service = SourceService(
                        session,
                        "personal",
                        AdapterRegistry(("official-document",)),
                    )
                    service.create(source())
                outcomes = []
                for offset in range(3):
                    with database.session() as session:
                        outcomes.append(
                            await OfficialDocumentCollectionService(
                                session,
                                "personal",
                                client,
                                (spec(),),
                            ).run(
                                "official-test",
                                since=NOW + timedelta(hours=offset),
                                until=NOW + timedelta(hours=offset + 1),
                            )
                        )
                assert [item.created_count for item in outcomes] == [1, 0, 1]
                with database.session() as session:
                    documents = session.query(RawDocumentRow).all()
                    assert len(documents) == 2
                    assert all(
                        item.metadata_payload["official_original_verified"] for item in documents
                    )
                    assert all(
                        item.metadata_payload["origin_provenance"]["verified_original"]
                        for item in documents
                    )
        finally:
            database.dispose()

    asyncio.run(scenario())
