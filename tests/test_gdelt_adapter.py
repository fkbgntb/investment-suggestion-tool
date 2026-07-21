from __future__ import annotations

import asyncio
import json
from datetime import UTC, datetime
from pathlib import Path

import httpx
import pytest

from app.collectors.gdelt import (
    FullTextFetchDisabled,
    GDELTAdapter,
    GDELTDailyLimitReached,
    GDELTQueryBuilder,
    discovered_to_raw_document,
)
from app.collectors.registry import AdapterRegistry
from app.collectors.safe_http import SafeFetchError, SafeHTTPClient
from app.domain.base import IdempotencyKey
from app.domain.contracts import SourceDiscoveryRequest, SourceFetchRequest
from app.domain.enums import (
    FetchErrorCode,
    SourceHealthStatus,
    SourceKind,
    TopicCategory,
    TrustTier,
)
from app.domain.taxonomy import Source, TaxonomyConfiguration, Topic
from app.services.gdelt_collection import GDELTCollectionService
from app.services.sources import SourceService
from app.services.taxonomy import TaxonomyService
from app.storage.database import Database
from app.storage.migrations import upgrade_database
from app.storage.repositories import RawDocumentRepository, SourceRepository, WorkspaceRepository

PUBLIC_IP = "93.184.216.34"


class StaticResolver:
    async def resolve(self, hostname: str, port: int) -> tuple[str, ...]:
        del hostname, port
        return (PUBLIC_IP,)


def topic(topic_id: str = "semiconductor") -> Topic:
    return Topic(
        topic_id=topic_id,
        name="Semiconductor",
        category=TopicCategory.THEME,
        aliases=("芯片",),
        keywords=("memory chip", "semiconductor equipment"),
        config_version="test-1",
    )


def request(*, cursor: str | None = None) -> SourceDiscoveryRequest:
    return SourceDiscoveryRequest(
        source_id="gdelt-global-news",
        topic_ids=("semiconductor",),
        since=datetime(2026, 7, 20, 0, tzinfo=UTC),
        until=datetime(2026, 7, 20, 3, tzinfo=UTC),
        cursor=cursor,
    )


def test_gdelt_policy_tolerates_slow_api_without_relaxing_content_bounds() -> None:
    policy = GDELTAdapter.url_policy("gdelt-global-news")

    assert policy.connect_timeout_seconds == 15
    assert policy.read_timeout_seconds == 60
    assert policy.total_timeout_seconds == 90
    assert policy.minimum_interval_seconds == 6
    assert policy.allowed_hosts == ("api.gdeltproject.org",)
    assert policy.allowed_content_types == ("application/json", "text/plain")
    assert policy.max_response_bytes == 2_000_000


def fixture_payload() -> dict[str, object]:
    return {
        "articles": [
            {
                "url": "https://News.Example.com/story?id=1#fragment",
                "title": "Memory supply remains tight",
                "seendate": "20260720T023000Z",
                "domain": "news.example.com",
                "language": "English",
                "sourcecountry": "United States",
                "summary": "Suppliers reported constrained inventory.",
            },
            {
                "url": "https://news.example.com/story?id=2",
                "title": "Chip equipment outlook",
                "seendate": "20260720T020000Z",
                "domain": "news.example.com",
                "language": "Chinese",
                "sourcecountry": "China",
            },
            {
                "url": "http://127.0.0.1/admin",
                "title": "Unsafe local link",
                "seendate": "20260720T010000Z",
            },
        ]
    }


def test_query_builder_is_topic_driven_bounded_and_cursor_aware() -> None:
    builder = GDELTQueryBuilder({"semiconductor": topic()})
    query = builder.build(request(cursor="2026-07-20T01:00:00Z"), max_records=50)
    decoded = httpx.URL(query.url)
    assert decoded.params["mode"] == "artlist"
    assert decoded.params["format"] == "json"
    assert decoded.params["sort"] == "dateasc"
    assert decoded.params["maxrecords"] == "50"
    assert decoded.params["startdatetime"] == "20260720010000"
    assert "Semiconductor" in decoded.params["query"]
    assert len(query.query_sha256) == 64


def test_query_builder_balances_topics_and_caps_expensive_or_terms() -> None:
    topics = {
        f"topic-{index}": topic(f"topic-{index}").model_copy(
            update={
                "name": f"Theme {index}",
                "aliases": (f"Alias {index}",),
                "keywords": (f"Keyword {index}",),
            }
        )
        for index in range(10)
    }
    value = request().model_copy(update={"topic_ids": tuple(topics)})
    query = GDELTQueryBuilder(topics).build(value, max_records=50)
    expression = httpx.URL(query.url).params["query"]

    assert expression.count(" OR ") + 1 == 16
    assert all(f'"Theme {index}"' in expression for index in range(10))
    assert len(expression) < 1_000


def test_fixed_response_parses_normalizes_and_rejects_local_links() -> None:
    documents, cursor = GDELTAdapter.parse_response(
        json.dumps(fixture_payload()).encode(),
        source_id="gdelt-global-news",
        discovered_at=datetime(2026, 7, 20, 3, tzinfo=UTC),
    )
    assert len(documents) == 2
    assert str(documents[0].source_url) == "https://news.example.com/story?id=1"
    assert documents[0].language == "en"
    assert documents[0].metadata["fulltext_fetched"] is False
    assert cursor == "2026-07-20T02:30:00Z"


def test_adapter_caps_results_tracks_summary_and_never_fetches_fulltext() -> None:
    async def scenario() -> None:
        transport = httpx.MockTransport(
            lambda request: httpx.Response(
                200,
                headers={"Content-Type": "application/json"},
                content=json.dumps(fixture_payload()).encode(),
                request=request,
            )
        )
        async with SafeHTTPClient(resolver=StaticResolver(), transport=transport) as client:
            adapter = GDELTAdapter(
                client,
                {"semiconductor": topic()},
                max_records=1,
                max_documents_per_day=1,
            )
            result = await adapter.discover(request())
            assert len(result.documents) == 1
            assert adapter.last_result_count == 1
            assert adapter.last_truncated is True
            assert adapter.last_query_sha256 is not None
            with pytest.raises(GDELTDailyLimitReached):
                await adapter.discover(request())
            with pytest.raises(FullTextFetchDisabled):
                await adapter.fetch(
                    SourceFetchRequest(
                        source_id="gdelt-global-news",
                        source_url="https://news.example.com/story",
                        idempotency=IdempotencyKey(
                            scope="fetch",
                            key="story-1",
                            payload_sha256="a" * 64,
                        ),
                    )
                )

    asyncio.run(scenario())


def test_network_failure_remains_a_sanitized_recognizable_error() -> None:
    async def fail(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("offline", request=request)

    async def scenario() -> None:
        async with SafeHTTPClient(
            resolver=StaticResolver(), transport=httpx.MockTransport(fail)
        ) as client:
            adapter = GDELTAdapter(client, {"semiconductor": topic()})
            with pytest.raises(SafeFetchError) as captured:
                await adapter.discover(request())
            assert captured.value.error_code is FetchErrorCode.NETWORK_ERROR
            assert "api.gdeltproject.org" not in str(captured.value)

    asyncio.run(scenario())


def test_materialized_metadata_is_idempotent_without_article_fulltext(tmp_path: Path) -> None:
    documents, _ = GDELTAdapter.parse_response(
        json.dumps(fixture_payload()).encode(),
        source_id="gdelt-global-news",
        discovered_at=datetime(2026, 7, 20, 3, tzinfo=UTC),
    )
    raw = discovered_to_raw_document(documents[0])
    assert raw.external.metadata["content_kind"] == "discovery_metadata"
    assert raw.external.metadata["fulltext_fetched"] is False

    database_url = f"sqlite:///{(tmp_path / 'gdelt.sqlite3').as_posix()}"
    upgrade_database(database_url)
    db = Database(database_url)
    try:
        with db.session() as session:
            WorkspaceRepository(session).create("personal", "Personal")
            SourceRepository(session, "personal").add(
                Source(
                    source_id="gdelt-global-news",
                    name="GDELT DOC 2.0",
                    kind=SourceKind.AGGREGATOR,
                    trust_tier=TrustTier.SECONDARY,
                    base_url="https://api.gdeltproject.org/api/v2/doc/doc",
                    regions=("global",),
                    languages=("multi",),
                    adapter_name="gdelt-doc",
                    allowed_domains=("api.gdeltproject.org",),
                )
            )
            repository = RawDocumentRepository(session, "personal")
            first, first_created = repository.add_if_absent(raw)
            second, second_created = repository.add_if_absent(raw)
            assert first_created is True
            assert second_created is False
            assert first.document_id == second.document_id
            assert first.metadata_payload["fulltext_fetched"] is False
    finally:
        db.dispose()


def test_collection_run_persists_summary_health_cursor_and_is_idempotent(tmp_path: Path) -> None:
    calls = 0

    def respond(request_value: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(
            200,
            headers={"Content-Type": "application/json"},
            content=json.dumps(fixture_payload()).encode(),
            request=request_value,
        )

    async def scenario() -> None:
        database_url = f"sqlite:///{(tmp_path / 'run.sqlite3').as_posix()}"
        upgrade_database(database_url)
        db = Database(database_url)
        try:
            async with SafeHTTPClient(
                resolver=StaticResolver(), transport=httpx.MockTransport(respond)
            ) as client:
                with db.session() as session:
                    TaxonomyService(session, "personal").publish(
                        TaxonomyConfiguration(
                            configuration_id="taxonomy-test",
                            config_version="test-1",
                            name="Test taxonomy",
                            topics=(topic(),),
                            created_at=datetime(2026, 7, 20, 0, tzinfo=UTC),
                        )
                    )
                    registered_source = Source(
                        source_id="gdelt-global-news",
                        name="GDELT DOC 2.0",
                        kind=SourceKind.AGGREGATOR,
                        trust_tier=TrustTier.SECONDARY,
                        base_url="https://api.gdeltproject.org/api/v2/doc/doc",
                        regions=("global",),
                        languages=("multi",),
                        adapter_name="gdelt-doc",
                        allowed_domains=("api.gdeltproject.org",),
                    )
                    SourceService(session, "personal", AdapterRegistry(("gdelt-doc",))).create(
                        registered_source
                    )
                    collector = GDELTCollectionService(
                        session,
                        "personal",
                        client,
                        max_records=10,
                        max_documents_per_day=10,
                    )
                    first = await collector.run(
                        registered_source.source_id,
                        since=datetime(2026, 7, 20, 0, tzinfo=UTC),
                        until=datetime(2026, 7, 20, 3, tzinfo=UTC),
                    )
                    second = await collector.run(
                        registered_source.source_id,
                        since=datetime(2026, 7, 20, 0, tzinfo=UTC),
                        until=datetime(2026, 7, 20, 3, tzinfo=UTC),
                    )
                    assert first.status == "SUCCEEDED"
                    assert first.created_count == 2
                    assert second.status == "SUCCEEDED"
                    assert second.created_count == 0
                    assert second.duplicate_count == 2
                    assert collector.sources.adapter_state(registered_source.source_id) is not None
                    assert (
                        collector.sources.health(registered_source.source_id).status
                        is SourceHealthStatus.HEALTHY
                    )
        finally:
            db.dispose()

    asyncio.run(scenario())
    assert calls == 2
