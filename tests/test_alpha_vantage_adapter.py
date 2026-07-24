from __future__ import annotations

import asyncio
import json
from datetime import UTC, datetime
from pathlib import Path

import httpx
import pytest
from pydantic import SecretStr

from app.collectors.alpha_vantage import (
    AlphaVantageAdapter,
    AlphaVantageFullTextFetchDisabled,
    AlphaVantageQueryBuilder,
    AlphaVantageRateLimitReached,
    discovered_to_raw_document,
)
from app.collectors.registry import AdapterRegistry
from app.collectors.safe_http import SafeHTTPClient
from app.domain.base import IdempotencyKey
from app.domain.contracts import SourceDiscoveryRequest, SourceFetchRequest
from app.domain.enums import SourceHealthStatus, SourceKind, TopicCategory, TrustTier
from app.domain.taxonomy import Source, TaxonomyConfiguration, Topic
from app.services.alpha_vantage_collection import AlphaVantageCollectionService
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


def topic() -> Topic:
    return Topic(
        topic_id="semiconductor",
        name="Semiconductor",
        category=TopicCategory.THEME,
        aliases=("chip",),
        keywords=("memory",),
        config_version="test-1",
    )


def request(*, cursor: str | None = None) -> SourceDiscoveryRequest:
    return SourceDiscoveryRequest(
        source_id="alpha-vantage-news",
        topic_ids=("semiconductor",),
        since=datetime(2026, 7, 20, 0, tzinfo=UTC),
        until=datetime(2026, 7, 20, 3, tzinfo=UTC),
        cursor=cursor,
    )


def fixture_payload() -> dict[str, object]:
    return {
        "items": "3",
        "feed": [
            {
                "title": "Memory supply remains tight",
                "url": "https://News.Example.com/story?id=1#fragment",
                "time_published": "20260720T023000",
                "summary": "Suppliers reported constrained inventory.",
                "source": "Example News",
                "source_domain": "news.example.com",
                "overall_sentiment_score": 0.21,
                "overall_sentiment_label": "Somewhat-Bullish",
                "topics": [{"topic": "Technology", "relevance_score": "0.9"}],
                "ticker_sentiment": [
                    {
                        "ticker": "MU",
                        "relevance_score": "0.8",
                        "ticker_sentiment_score": "0.2",
                    }
                ],
            },
            {
                "title": "Chip equipment outlook",
                "url": "https://news.example.com/story?id=2",
                "time_published": "20260720T020000",
                "source": "Example News",
            },
            {
                "title": "Unsafe local link",
                "url": "http://127.0.0.1/admin",
                "time_published": "20260720T010000",
            },
        ],
    }


def test_query_is_bounded_cursor_aware_and_keeps_key_out_of_stored_parameters() -> None:
    query = AlphaVantageQueryBuilder().build(request(cursor="2026-07-20T01:00:00Z"), max_records=50)
    parameters = dict(query.parameters)

    assert parameters == {
        "function": "NEWS_SENTIMENT",
        "tickers": query.focus_ticker,
        "time_from": "20260720T0100",
        "time_to": "20260720T0300",
        "sort": "LATEST",
        "limit": "50",
    }
    assert "apikey" not in parameters
    assert query.focus_ticker in {"MU", "TSM", "ASML", "NVDA"}
    assert "," not in parameters["tickers"]
    assert "local-secret" in query.authenticated_url(SecretStr("local-secret"))
    assert len(query.query_sha256) == 64


def test_policy_pins_official_host_and_bounded_response() -> None:
    policy = AlphaVantageAdapter.url_policy("alpha-vantage-news")

    assert policy.allowed_hosts == ("www.alphavantage.co",)
    assert policy.allowed_content_types == ("application/json",)
    assert policy.max_response_bytes == 2_000_000
    assert policy.minimum_interval_seconds == 6
    assert policy.total_timeout_seconds == 90


def test_response_parses_metadata_and_rejects_local_links() -> None:
    documents, cursor = AlphaVantageAdapter.parse_response(
        json.dumps(fixture_payload()).encode(),
        source_id="alpha-vantage-news",
        discovered_at=datetime(2026, 7, 20, 3, tzinfo=UTC),
    )

    assert len(documents) == 2
    assert str(documents[0].source_url) == "https://news.example.com/story?id=1"
    assert documents[0].metadata["overall_sentiment_label"] == "Somewhat-Bullish"
    assert documents[0].metadata["ticker_sentiment"][0]["ticker"] == "MU"
    assert documents[0].metadata["origin_provenance"]["original_domain"] == "news.example.com"
    assert documents[0].metadata["origin_provenance"]["verified_original"] is False
    assert cursor == "2026-07-20T02:30:00Z"


def test_query_rotates_one_ticker_per_three_hour_window() -> None:
    builder = AlphaVantageQueryBuilder()
    values = []
    for offset in range(4):
        value = request().model_copy(
            update={
                "since": datetime(2026, 7, 20, offset * 3, tzinfo=UTC),
                "until": datetime(2026, 7, 20, offset * 3 + 3, tzinfo=UTC),
            }
        )
        query = builder.build(value, max_records=50)
        values.append(dict(query.parameters)["tickers"])
    assert set(values) == {"MU", "TSM", "ASML", "NVDA"}
    assert all("," not in value for value in values)


def test_in_band_rate_limit_is_recognizable() -> None:
    payload = {"Information": "The standard API rate limit is 25 requests per day."}

    with pytest.raises(AlphaVantageRateLimitReached):
        AlphaVantageAdapter.parse_response(
            json.dumps(payload).encode(),
            source_id="alpha-vantage-news",
            discovered_at=datetime(2026, 7, 20, 3, tzinfo=UTC),
        )


def test_adapter_uses_key_only_for_request_and_never_fetches_fulltext() -> None:
    seen_url = ""

    def respond(value: httpx.Request) -> httpx.Response:
        nonlocal seen_url
        seen_url = str(value.url)
        return httpx.Response(
            200,
            headers={"Content-Type": "application/json"},
            content=json.dumps(fixture_payload()).encode(),
            request=value,
        )

    async def scenario() -> None:
        async with SafeHTTPClient(
            resolver=StaticResolver(), transport=httpx.MockTransport(respond)
        ) as client:
            adapter = AlphaVantageAdapter(client, SecretStr("test-secret"), max_records=1)
            result = await adapter.discover(request())
            assert len(result.documents) == 1
            assert adapter.last_truncated is True
            with pytest.raises(AlphaVantageFullTextFetchDisabled):
                await adapter.fetch(
                    SourceFetchRequest(
                        source_id="alpha-vantage-news",
                        source_url="https://news.example.com/story",
                        idempotency=IdempotencyKey(
                            scope="fetch",
                            key="story-1",
                            payload_sha256="a" * 64,
                        ),
                    )
                )

    asyncio.run(scenario())
    assert "apikey=test-secret" in seen_url


def test_materialized_metadata_is_idempotent(tmp_path: Path) -> None:
    documents, _ = AlphaVantageAdapter.parse_response(
        json.dumps(fixture_payload()).encode(),
        source_id="alpha-vantage-news",
        discovered_at=datetime(2026, 7, 20, 3, tzinfo=UTC),
    )
    raw = discovered_to_raw_document(documents[0])
    assert raw.control.document_id.startswith("alpha-vantage-")
    assert raw.external.metadata["fulltext_fetched"] is False

    database_url = f"sqlite:///{(tmp_path / 'alpha.sqlite3').as_posix()}"
    upgrade_database(database_url)
    db = Database(database_url)
    try:
        with db.session() as session:
            WorkspaceRepository(session).create("personal", "Personal")
            SourceRepository(session, "personal").add(source())
            repository = RawDocumentRepository(session, "personal")
            first, first_created = repository.add_if_absent(raw)
            second, second_created = repository.add_if_absent(raw)
            assert first_created is True
            assert second_created is False
            assert first.document_id == second.document_id
    finally:
        db.dispose()


def source() -> Source:
    return Source(
        source_id="alpha-vantage-news",
        name="Alpha Vantage News & Sentiment",
        kind=SourceKind.AGGREGATOR,
        trust_tier=TrustTier.SECONDARY,
        base_url="https://www.alphavantage.co/query",
        regions=("global",),
        languages=("en",),
        adapter_name="alpha-vantage-news",
        allowed_domains=("www.alphavantage.co",),
    )


def test_collection_persists_summary_health_cursor_and_daily_call_limit(tmp_path: Path) -> None:
    calls = 0

    def respond(value: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(
            200,
            headers={"Content-Type": "application/json"},
            content=json.dumps(fixture_payload()).encode(),
            request=value,
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
                            created_at=datetime(2026, 7, 20, tzinfo=UTC),
                        )
                    )
                    registered = source()
                    SourceService(
                        session,
                        "personal",
                        AdapterRegistry(("alpha-vantage-news",)),
                    ).create(registered)
                    collector = AlphaVantageCollectionService(
                        session,
                        "personal",
                        client,
                        SecretStr("test-secret"),
                        max_records=10,
                        max_calls_per_day=1,
                    )
                    first = await collector.run(
                        registered.source_id,
                        since=datetime(2026, 7, 20, 0, tzinfo=UTC),
                        until=datetime(2026, 7, 20, 3, tzinfo=UTC),
                    )
                    limited = await collector.run(
                        registered.source_id,
                        since=datetime(2026, 7, 20, 3, tzinfo=UTC),
                        until=datetime(2026, 7, 20, 6, tzinfo=UTC),
                    )
                    assert first.status == "SUCCEEDED"
                    assert first.created_count == 2
                    assert limited.error_code == "DAILY_LIMIT_REACHED"
                    assert (
                        collector.sources.health(registered.source_id).status
                        is SourceHealthStatus.DEGRADED
                    )
        finally:
            db.dispose()

    asyncio.run(scenario())
    assert calls == 1
