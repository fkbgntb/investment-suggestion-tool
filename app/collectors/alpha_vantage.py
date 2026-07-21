"""Bounded Alpha Vantage NEWS_SENTIMENT discovery adapter."""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from datetime import UTC, datetime
from hashlib import sha256
from ipaddress import ip_address
from urllib.parse import urlencode, urlsplit, urlunsplit

from pydantic import SecretStr, ValidationError

from app.collectors.safe_http import SafeHTTPClient
from app.domain.base import IdempotencyKey
from app.domain.collection import URLPolicy
from app.domain.contracts import (
    SourceDiscoveryRequest,
    SourceDiscoveryResult,
    SourceFetchRequest,
    SourceFetchResult,
)
from app.domain.documents import (
    DiscoveredDocument,
    ExternalDocumentContent,
    RawDocument,
    RawDocumentControl,
)
from app.domain.enums import DocumentState

ALPHA_VANTAGE_ENDPOINT = "https://www.alphavantage.co/query"
_PUBLISHED_TIME = "%Y%m%dT%H%M%S"
_CONTROL = re.compile(r"[\x00-\x1f\x7f]")
_RATE_LIMIT_MARKERS = (
    "rate limit",
    "call frequency",
    "requests per day",
    "api call frequency",
)


class AlphaVantageFullTextFetchDisabled(RuntimeError):
    """Discovery result links are untrusted and are not fetched by this adapter."""


class AlphaVantageResponseError(ValueError):
    """The bounded upstream response did not match the documented JSON shape."""


class AlphaVantageRateLimitReached(RuntimeError):
    """Alpha Vantage returned an in-band quota response with HTTP 200."""


@dataclass(frozen=True)
class AlphaVantageQuery:
    parameters: tuple[tuple[str, str], ...]
    query_sha256: str
    effective_since: datetime

    def authenticated_url(self, api_key: SecretStr) -> str:
        values = [*self.parameters, ("apikey", api_key.get_secret_value())]
        return f"{ALPHA_VANTAGE_ENDPOINT}?{urlencode(values)}"


class AlphaVantageQueryBuilder:
    """Build one technology-news request without embedding credentials in stored state."""

    def build(self, request: SourceDiscoveryRequest, *, max_records: int) -> AlphaVantageQuery:
        effective_since = request.since.astimezone(UTC)
        if request.cursor is not None:
            effective_since = max(effective_since, self._parse_cursor(request.cursor))
        until = request.until.astimezone(UTC)
        if effective_since > until:
            raise ValueError("Alpha Vantage cursor cannot follow the request end time")

        parameters = (
            ("function", "NEWS_SENTIMENT"),
            ("topics", "technology"),
            ("time_from", effective_since.strftime("%Y%m%dT%H%M")),
            ("time_to", until.strftime("%Y%m%dT%H%M")),
            ("sort", "LATEST"),
            ("limit", str(max_records)),
        )
        digest_input = urlencode(parameters).encode("utf-8")
        return AlphaVantageQuery(
            parameters=parameters,
            query_sha256=sha256(digest_input).hexdigest(),
            effective_since=effective_since,
        )

    @staticmethod
    def _parse_cursor(value: str) -> datetime:
        try:
            parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError as error:
            raise ValueError("Alpha Vantage cursor must be an ISO-8601 timestamp") from error
        if parsed.tzinfo is None:
            raise ValueError("Alpha Vantage cursor must include a timezone")
        return parsed.astimezone(UTC)


class AlphaVantageAdapter:
    adapter_name = "alpha-vantage-news"

    def __init__(
        self,
        http_client: SafeHTTPClient,
        api_key: SecretStr,
        *,
        max_records: int = 50,
    ) -> None:
        if not api_key.get_secret_value().strip():
            raise ValueError("Alpha Vantage API key cannot be empty")
        if max_records < 1 or max_records > 1_000:
            raise ValueError("Alpha Vantage max_records must be between 1 and 1000")
        self._http = http_client
        self._api_key = api_key
        self._query_builder = AlphaVantageQueryBuilder()
        self._max_records = max_records
        self.last_query_sha256: str | None = None
        self.last_result_count = 0
        self.last_truncated = False

    async def discover(self, request: SourceDiscoveryRequest) -> SourceDiscoveryResult:
        query = self._query_builder.build(request, max_records=self._max_records)
        self.last_query_sha256 = query.query_sha256
        response = await self._http.fetch(
            query.authenticated_url(self._api_key),
            self.url_policy(request.source_id),
        )
        documents, next_cursor = self.parse_response(
            response.body,
            source_id=request.source_id,
            discovered_at=request.until.astimezone(UTC),
        )
        limited = documents[: self._max_records]
        self.last_result_count = len(limited)
        self.last_truncated = len(limited) < len(documents)
        return SourceDiscoveryResult(documents=limited, next_cursor=next_cursor)

    async def fetch(self, request: SourceFetchRequest) -> SourceFetchResult:
        del request
        raise AlphaVantageFullTextFetchDisabled(
            "Alpha Vantage result links require a separately approved adapter"
        )

    @staticmethod
    def url_policy(source_id: str) -> URLPolicy:
        return URLPolicy(
            source_id=source_id,
            allowed_hosts=("www.alphavantage.co",),
            allowed_content_types=("application/json",),
            max_response_bytes=2_000_000,
            minimum_interval_seconds=6,
            connect_timeout_seconds=15,
            read_timeout_seconds=60,
            total_timeout_seconds=90,
        )

    @classmethod
    def parse_response(
        cls,
        body: bytes,
        *,
        source_id: str,
        discovered_at: datetime,
    ) -> tuple[tuple[DiscoveredDocument, ...], str | None]:
        try:
            payload = json.loads(body)
        except (UnicodeDecodeError, json.JSONDecodeError) as error:
            raise AlphaVantageResponseError(
                "Alpha Vantage response is not valid UTF-8 JSON"
            ) from error
        if not isinstance(payload, dict):
            raise AlphaVantageResponseError("Alpha Vantage response is not an object")

        informational = payload.get("Note") or payload.get("Information")
        if isinstance(informational, str) and any(
            marker in informational.casefold() for marker in _RATE_LIMIT_MARKERS
        ):
            raise AlphaVantageRateLimitReached("Alpha Vantage daily or frequency limit reached")
        feed = payload.get("feed")
        if not isinstance(feed, list):
            raise AlphaVantageResponseError("Alpha Vantage response does not contain a news feed")

        documents: list[DiscoveredDocument] = []
        newest: datetime | None = None
        seen_urls: set[str] = set()
        for raw in feed[:1_000]:
            if not isinstance(raw, dict):
                continue
            document = cls._parse_article(raw, source_id=source_id, discovered_at=discovered_at)
            if document is None:
                continue
            normalized_url = str(document.source_url)
            if normalized_url in seen_urls:
                continue
            seen_urls.add(normalized_url)
            documents.append(document)
            if document.published_at is not None:
                newest = max(newest, document.published_at) if newest else document.published_at
        cursor = newest.astimezone(UTC).isoformat().replace("+00:00", "Z") if newest else None
        return tuple(documents), cursor

    @classmethod
    def _parse_article(
        cls,
        raw: dict[str, object],
        *,
        source_id: str,
        discovered_at: datetime,
    ) -> DiscoveredDocument | None:
        url = cls._normalize_untrusted_url(raw.get("url"))
        title = cls._bounded_text(raw.get("title"), 1_000)
        if url is None or not title:
            return None
        published_at = cls._parse_published_time(raw.get("time_published"))
        summary = cls._bounded_text(raw.get("summary"), 20_000)
        publisher = cls._bounded_text(raw.get("source"), 300)
        metadata = {
            "provider": "alpha_vantage",
            "source_domain": cls._bounded_text(raw.get("source_domain"), 300),
            "overall_sentiment_score": cls._bounded_text(raw.get("overall_sentiment_score"), 40),
            "overall_sentiment_label": cls._bounded_text(raw.get("overall_sentiment_label"), 100),
            "topics": cls._bounded_scores(raw.get("topics"), name_key="topic"),
            "ticker_sentiment": cls._bounded_scores(raw.get("ticker_sentiment"), name_key="ticker"),
            "fulltext_fetched": False,
        }
        try:
            return DiscoveredDocument(
                source_id=source_id,
                source_url=url,
                discovered_at=discovered_at,
                external_reference=sha256(url.encode("utf-8")).hexdigest(),
                title=title,
                summary=summary,
                publisher=publisher,
                language="en",
                published_at=published_at,
                metadata=metadata,
            )
        except ValidationError:
            return None

    @classmethod
    def _bounded_scores(cls, value: object, *, name_key: str) -> list[dict[str, str]]:
        if not isinstance(value, list):
            return []
        result: list[dict[str, str]] = []
        for item in value[:20]:
            if not isinstance(item, dict):
                continue
            name = cls._bounded_text(item.get(name_key), 100)
            relevance = cls._bounded_text(item.get("relevance_score"), 40)
            sentiment = cls._bounded_text(item.get("ticker_sentiment_score"), 40)
            if name is None:
                continue
            bounded = {name_key: name}
            if relevance is not None:
                bounded["relevance_score"] = relevance
            if sentiment is not None:
                bounded["sentiment_score"] = sentiment
            result.append(bounded)
        return result

    @staticmethod
    def _bounded_text(value: object, maximum: int) -> str | None:
        if not isinstance(value, (str, int, float)) or isinstance(value, bool):
            return None
        cleaned = " ".join(_CONTROL.sub(" ", str(value)).split())
        return cleaned[:maximum] or None

    @staticmethod
    def _parse_published_time(value: object) -> datetime | None:
        if not isinstance(value, str):
            return None
        try:
            return datetime.strptime(value, _PUBLISHED_TIME).replace(tzinfo=UTC)
        except ValueError:
            return None

    @staticmethod
    def _normalize_untrusted_url(value: object) -> str | None:
        if not isinstance(value, str) or len(value) > 4_096:
            return None
        parsed = urlsplit(value.strip())
        if parsed.scheme.casefold() not in {"http", "https"} or not parsed.hostname:
            return None
        if parsed.username is not None or parsed.password is not None:
            return None
        try:
            hostname = parsed.hostname.rstrip(".").encode("idna").decode("ascii").casefold()
        except UnicodeError:
            return None
        if hostname == "localhost" or hostname.endswith(".local"):
            return None
        try:
            if not ip_address(hostname).is_global:
                return None
        except ValueError:
            pass
        try:
            port = parsed.port
        except ValueError:
            return None
        if port is not None and port not in {80, 443}:
            return None
        netloc = hostname if port is None else f"{hostname}:{port}"
        return urlunsplit((parsed.scheme.casefold(), netloc, parsed.path or "/", parsed.query, ""))


def discovered_to_raw_document(document: DiscoveredDocument) -> RawDocument:
    """Materialize bounded Alpha Vantage metadata without fetching article full text."""

    if document.title is None:
        raise ValueError("a discovered title is required for raw document materialization")
    canonical_url = str(document.source_url)
    url_digest = sha256(canonical_url.encode("utf-8")).hexdigest()
    content = ExternalDocumentContent(
        source_url=document.source_url,
        title=document.title,
        body=document.summary or document.title,
        published_at=document.published_at,
        author=document.publisher,
        language=document.language or "en",
        metadata={
            **document.metadata,
            "content_kind": "discovery_metadata",
            "summary_available": document.summary is not None,
        },
    )
    return RawDocument(
        external=content,
        control=RawDocumentControl(
            document_id=f"alpha-vantage-{url_digest[:32]}",
            source_id=document.source_id,
            state=DocumentState.DISCOVERED,
            state_version=0,
            content_sha256=url_digest,
            idempotency=IdempotencyKey(
                scope="alpha-vantage-discovery",
                key=url_digest,
                payload_sha256=url_digest,
            ),
            discovered_at=document.discovered_at,
        ),
    )
