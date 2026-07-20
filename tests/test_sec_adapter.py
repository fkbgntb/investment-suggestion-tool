from __future__ import annotations

import asyncio
import json
from datetime import UTC, datetime
from pathlib import Path

import httpx
import pytest

from app.collectors.registry import AdapterRegistry
from app.collectors.safe_http import SafeFetchError, SafeHTTPClient
from app.collectors.sec import (
    SECAdapter,
    SECCompany,
    SECConfigurationError,
    sec_discovery_to_raw_document,
)
from app.domain.base import IdempotencyKey
from app.domain.contracts import SourceDiscoveryRequest, SourceFetchRequest
from app.domain.enums import EntityType, FetchErrorCode, SourceKind, TopicCategory, TrustTier
from app.domain.taxonomy import Entity, Source, TaxonomyConfiguration, Topic
from app.services.sec_collection import SECCollectionService
from app.services.sources import SourceService
from app.services.taxonomy import TaxonomyService
from app.storage.database import Database
from app.storage.migrations import upgrade_database
from app.storage.repositories import RawDocumentRepository, SourceRepository, WorkspaceRepository

PUBLIC_IP = "23.62.74.94"


class StaticResolver:
    async def resolve(self, hostname: str, port: int) -> tuple[str, ...]:
        del hostname, port
        return (PUBLIC_IP,)


def company() -> SECCompany:
    return SECCompany(
        entity_id="company.nvidia",
        cik="0001045810",
        forms=("10-K", "10-Q", "8-K"),
    )


def request() -> SourceDiscoveryRequest:
    return SourceDiscoveryRequest(
        source_id="sec-company-filings",
        topic_ids=("semiconductor",),
        since=datetime(2026, 5, 1, tzinfo=UTC),
        until=datetime(2026, 5, 31, tzinfo=UTC),
    )


def submissions_payload() -> dict[str, object]:
    return {
        "cik": "0001045810",
        "name": "NVIDIA CORP",
        "filings": {
            "recent": {
                "accessionNumber": [
                    "0001045810-26-000123",
                    "0001045810-26-000122",
                    "0001045810-26-000121",
                ],
                "filingDate": ["2026-05-20", "2026-05-15", "2026-05-08"],
                "acceptanceDateTime": [
                    "2026-05-20T16:30:00-04:00",
                    "2026-05-15T12:00:00-04:00",
                    "2026-05-08T09:00:00-04:00",
                ],
                "form": ["10-Q", "13F-HR", "8-K"],
                "primaryDocument": ["nvda-20260426.htm", "holdings.xml", "nvda-8k.htm"],
                "primaryDocDescription": ["Quarterly report", "Holdings", "Current report"],
            }
        },
    }


def test_fixed_sec_sample_filters_forms_and_builds_only_official_links() -> None:
    documents = SECAdapter.parse_submissions(
        json.dumps(submissions_payload()).encode(),
        source_id="sec-company-filings",
        company=company(),
        since=datetime(2026, 5, 1, tzinfo=UTC),
        until=datetime(2026, 5, 31, tzinfo=UTC),
        discovered_at=datetime(2026, 5, 31, tzinfo=UTC),
        maximum=10,
    )
    assert len(documents) == 2
    assert documents[0].external_reference == "0001045810-26-000123"
    assert documents[0].metadata["form"] == "10-Q"
    assert str(documents[0].source_url).startswith(
        "https://www.sec.gov/Archives/edgar/data/1045810/"
    )
    assert documents[0].metadata["direct_etf_impact_unverified"] is True


def test_discovery_declares_user_agent_and_respects_result_limit() -> None:
    seen_user_agents: list[str] = []

    def respond(request_value: httpx.Request) -> httpx.Response:
        seen_user_agents.append(request_value.headers["User-Agent"])
        return httpx.Response(
            200,
            headers={"Content-Type": "application/json"},
            content=json.dumps(submissions_payload()).encode(),
            request=request_value,
        )

    async def scenario() -> None:
        async with SafeHTTPClient(
            resolver=StaticResolver(), transport=httpx.MockTransport(respond)
        ) as client:
            adapter = SECAdapter(
                client,
                (company(),),
                contact_email="researcher@example.com",
                max_filings_per_company=1,
            )
            result = await adapter.discover(request())
            assert len(result.documents) == 1
            assert result.documents[0].metadata["form"] == "10-Q"

    asyncio.run(scenario())
    assert seen_user_agents == ["investment-suggestion-tool/0.1 researcher@example.com"]


def test_fulltext_fetch_is_limited_to_sec_document_host() -> None:
    async def scenario() -> None:
        transport = httpx.MockTransport(
            lambda request_value: httpx.Response(
                200,
                headers={"Content-Type": "text/html"},
                content=b"<html><body>untrusted filing text</body></html>",
                request=request_value,
            )
        )
        async with SafeHTTPClient(resolver=StaticResolver(), transport=transport) as client:
            adapter = SECAdapter(
                client,
                (company(),),
                contact_email="researcher@example.com",
            )
            idempotency = IdempotencyKey(
                scope="sec-fetch",
                key="filing-1",
                payload_sha256="a" * 64,
            )
            fetched = await adapter.fetch(
                SourceFetchRequest(
                    source_id="sec-company-filings",
                    source_url=(
                        "https://www.sec.gov/Archives/edgar/data/1045810/"
                        "000104581026000123/nvda-20260426.htm"
                    ),
                    idempotency=idempotency,
                )
            )
            assert fetched.content.metadata["untrusted_filing_text"] is True
            with pytest.raises(SafeFetchError) as captured:
                await adapter.fetch(
                    SourceFetchRequest(
                        source_id="sec-company-filings",
                        source_url="https://evil.example/attachment.exe",
                        idempotency=idempotency,
                    )
                )
            assert captured.value.error_code is FetchErrorCode.HOST_REJECTED

    asyncio.run(scenario())


def test_contact_and_company_configuration_fail_closed() -> None:
    with pytest.raises(SECConfigurationError, match="contact email"):
        SECAdapter(object(), (company(),), contact_email="missing-at-sign")
    with pytest.raises(ValueError, match="form names"):
        SECCompany(entity_id="company.nvidia", cik="0001045810", forms=("10-Q;DROP",))


def test_accession_materialization_is_idempotent(tmp_path: Path) -> None:
    document = SECAdapter.parse_submissions(
        json.dumps(submissions_payload()).encode(),
        source_id="sec-company-filings",
        company=company(),
        since=datetime(2026, 5, 1, tzinfo=UTC),
        until=datetime(2026, 5, 31, tzinfo=UTC),
        discovered_at=datetime(2026, 5, 31, tzinfo=UTC),
        maximum=1,
    )[0]
    raw = sec_discovery_to_raw_document(document)
    assert raw.external.metadata["accession_number"] == "0001045810-26-000123"

    database_url = f"sqlite:///{(tmp_path / 'sec.sqlite3').as_posix()}"
    upgrade_database(database_url)
    db = Database(database_url)
    try:
        with db.session() as session:
            WorkspaceRepository(session).create("personal", "Personal")
            SourceRepository(session, "personal").add(
                Source(
                    source_id="sec-company-filings",
                    name="SEC EDGAR",
                    kind=SourceKind.REGULATOR,
                    trust_tier=TrustTier.PRIMARY,
                    base_url="https://data.sec.gov/submissions/",
                    regions=("us",),
                    languages=("en",),
                    adapter_name="sec-submissions",
                    allowed_domains=("data.sec.gov", "www.sec.gov"),
                )
            )
            repository = RawDocumentRepository(session, "personal")
            first, created_first = repository.add_if_absent(raw)
            second, created_second = repository.add_if_absent(raw)
            assert created_first is True
            assert created_second is False
            assert first.document_id == second.document_id
    finally:
        db.dispose()


def test_sec_collection_run_persists_summary_cursor_and_health(tmp_path: Path) -> None:
    calls = 0

    def respond(request_value: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(
            200,
            headers={"Content-Type": "application/json"},
            content=json.dumps(submissions_payload()).encode(),
            request=request_value,
        )

    async def scenario() -> None:
        database_url = f"sqlite:///{(tmp_path / 'sec-run.sqlite3').as_posix()}"
        upgrade_database(database_url)
        db = Database(database_url)
        try:
            async with SafeHTTPClient(
                resolver=StaticResolver(), transport=httpx.MockTransport(respond)
            ) as client:
                with db.session() as session:
                    TaxonomyService(session, "personal").publish(
                        TaxonomyConfiguration(
                            configuration_id="taxonomy-sec-test",
                            config_version="sec-test-1",
                            name="SEC test taxonomy",
                            topics=(
                                Topic(
                                    topic_id="semiconductor",
                                    name="Semiconductor",
                                    category=TopicCategory.THEME,
                                    config_version="sec-test-1",
                                ),
                            ),
                            entities=(
                                Entity(
                                    entity_id="company.nvidia",
                                    name="NVIDIA",
                                    entity_type=EntityType.COMPANY,
                                    config_version="sec-test-1",
                                ),
                            ),
                            created_at=datetime(2026, 5, 1, tzinfo=UTC),
                        )
                    )
                    source = Source(
                        source_id="sec-company-filings",
                        name="SEC EDGAR",
                        kind=SourceKind.REGULATOR,
                        trust_tier=TrustTier.PRIMARY,
                        base_url="https://data.sec.gov/submissions/",
                        regions=("us",),
                        languages=("en",),
                        adapter_name="sec-submissions",
                        allowed_domains=("data.sec.gov", "www.sec.gov"),
                    )
                    SourceService(
                        session,
                        "personal",
                        AdapterRegistry(("sec-submissions",)),
                    ).create(source)
                    service = SECCollectionService(
                        session,
                        "personal",
                        client,
                        (company(),),
                        contact_email="researcher@example.com",
                    )
                    first = await service.run(
                        source.source_id,
                        since=datetime(2026, 5, 1, tzinfo=UTC),
                        until=datetime(2026, 5, 31, tzinfo=UTC),
                    )
                    assert first.status == "SUCCEEDED"
                    assert first.created_count == 2
                    assert service.sources.adapter_state(source.source_id) is not None
                    assert service.sources.health(source.source_id).status.value == "HEALTHY"
        finally:
            db.dispose()

    asyncio.run(scenario())
    assert calls == 1
