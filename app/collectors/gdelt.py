"""Bounded GDELT DOC 2.0 discovery adapter for global news metadata."""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from datetime import UTC, date, datetime
from hashlib import sha256
from ipaddress import ip_address
from urllib.parse import urlencode, urlsplit, urlunsplit

from pydantic import ValidationError

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
from app.domain.taxonomy import Topic

GDELT_ENDPOINT = "https://api.gdeltproject.org/api/v2/doc/doc"
_GDELT_TIME = "%Y%m%dT%H%M%SZ"
_MAX_QUERY_TERMS = 16
_CONTROL = re.compile(r"[\x00-\x1f\x7f]")
_LANGUAGE_CODES = {
    "arabic": "ar",
    "chinese": "zh",
    "english": "en",
    "french": "fr",
    "german": "de",
    "italian": "it",
    "japanese": "ja",
    "korean": "ko",
    "portuguese": "pt",
    "russian": "ru",
    "spanish": "es",
}


class FullTextFetchDisabled(RuntimeError):
    """GDELT discovery links are untrusted and are not fetched by this adapter."""


class GDELTResponseError(ValueError):
    """The upstream response was bounded but did not match the expected JSON shape."""


class GDELTDailyLimitReached(RuntimeError):
    """The process-local daily discovery budget has been exhausted."""


@dataclass(frozen=True)
class GDELTQuery:
    url: str
    query_sha256: str
    effective_since: datetime


class DailyDocumentBudget:
    """Small process-local guard; persistent enforcement is added with the scheduler."""

    def __init__(self, maximum: int) -> None:
        if maximum < 1:
            raise ValueError("daily document maximum must be positive")
        self.maximum = maximum
        self._day: date | None = None
        self._used = 0

    def take(self, requested: int, *, today: date) -> int:
        if requested < 0:
            raise ValueError("requested document count cannot be negative")
        if requested == 0:
            return 0
        if self._day != today:
            self._day = today
            self._used = 0
        remaining = self.maximum - self._used
        if remaining <= 0:
            raise GDELTDailyLimitReached("GDELT daily document limit reached")
        accepted = min(requested, remaining)
        self._used += accepted
        return accepted


class GDELTQueryBuilder:
    def __init__(self, topics: dict[str, Topic]) -> None:
        self._topics = topics

    def build(self, request: SourceDiscoveryRequest, *, max_records: int) -> GDELTQuery:
        candidates_by_topic: list[tuple[str, ...]] = []
        for topic_id in request.topic_ids:
            topic = self._topics.get(topic_id)
            if topic is None or not topic.enabled:
                raise ValueError(f"unknown or disabled topic: {topic_id}")
            candidates = (topic.name, *topic.aliases, *topic.keywords)
            candidates_by_topic.append(
                tuple(
                    sanitized
                    for candidate in candidates[:8]
                    if (sanitized := self._sanitize_term(candidate))
                )
            )
        terms: list[str] = []
        normalized_terms: set[str] = set()
        maximum_candidates = max((len(items) for items in candidates_by_topic), default=0)
        for candidate_index in range(maximum_candidates):
            for candidates in candidates_by_topic:
                if candidate_index >= len(candidates):
                    continue
                candidate = candidates[candidate_index]
                normalized = candidate.casefold()
                if normalized in normalized_terms:
                    continue
                terms.append(candidate)
                normalized_terms.add(normalized)
                if len(terms) >= _MAX_QUERY_TERMS:
                    break
            if len(terms) >= _MAX_QUERY_TERMS:
                break
        if not terms:
            raise ValueError("at least one safe query term is required")
        query = "(" + " OR ".join(f'"{term}"' for term in terms) + ")"
        if len(query) > 1_000:
            raise ValueError("GDELT query exceeds the bounded length")

        effective_since = request.since.astimezone(UTC)
        if request.cursor is not None:
            cursor_time = self._parse_cursor(request.cursor)
            effective_since = max(effective_since, cursor_time)
        if effective_since > request.until.astimezone(UTC):
            raise ValueError("GDELT cursor cannot follow the request end time")
        parameters = {
            "query": query,
            "mode": "artlist",
            "maxrecords": str(max_records),
            "format": "json",
            "sort": "dateasc",
            "startdatetime": effective_since.strftime("%Y%m%d%H%M%S"),
            "enddatetime": request.until.astimezone(UTC).strftime("%Y%m%d%H%M%S"),
        }
        return GDELTQuery(
            url=f"{GDELT_ENDPOINT}?{urlencode(parameters)}",
            query_sha256=sha256(query.encode("utf-8")).hexdigest(),
            effective_since=effective_since,
        )

    @staticmethod
    def _sanitize_term(value: str) -> str:
        cleaned = _CONTROL.sub(" ", value).replace("\\", " ").replace('"', " ")
        return " ".join(cleaned.split())[:100]

    @staticmethod
    def _parse_cursor(value: str) -> datetime:
        try:
            parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError as error:
            raise ValueError("GDELT cursor must be an ISO-8601 timestamp") from error
        if parsed.tzinfo is None:
            raise ValueError("GDELT cursor must include a timezone")
        return parsed.astimezone(UTC)


class GDELTAdapter:
    adapter_name = "gdelt-doc"

    def __init__(
        self,
        http_client: SafeHTTPClient,
        topics: dict[str, Topic],
        *,
        max_records: int = 50,
        max_documents_per_day: int = 500,
    ) -> None:
        if max_records < 1 or max_records > 250:
            raise ValueError("GDELT max_records must be between 1 and 250")
        self._http = http_client
        self._query_builder = GDELTQueryBuilder(topics)
        self._max_records = max_records
        self._budget = DailyDocumentBudget(max_documents_per_day)
        self.last_query_sha256: str | None = None
        self.last_result_count = 0
        self.last_truncated = False

    async def discover(self, request: SourceDiscoveryRequest) -> SourceDiscoveryResult:
        query = self._query_builder.build(request, max_records=self._max_records)
        self.last_query_sha256 = query.query_sha256
        response = await self._http.fetch(query.url, self.url_policy(request.source_id))
        documents, next_cursor = self.parse_response(
            response.body,
            source_id=request.source_id,
            discovered_at=request.until.astimezone(UTC),
        )
        accepted = self._budget.take(len(documents), today=request.until.astimezone(UTC).date())
        limited = documents[: min(accepted, self._max_records)]
        self.last_result_count = len(limited)
        self.last_truncated = len(limited) < len(documents)
        return SourceDiscoveryResult(documents=limited, next_cursor=next_cursor)

    async def fetch(self, request: SourceFetchRequest) -> SourceFetchResult:
        del request
        raise FullTextFetchDisabled("GDELT result links require a separately approved adapter")

    @staticmethod
    def url_policy(source_id: str) -> URLPolicy:
        return URLPolicy(
            source_id=source_id,
            allowed_hosts=("api.gdeltproject.org",),
            allowed_content_types=("application/json", "text/plain"),
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
            raise GDELTResponseError("GDELT response is not valid UTF-8 JSON") from error
        if not isinstance(payload, dict) or not isinstance(payload.get("articles", []), list):
            raise GDELTResponseError("GDELT response does not contain an article list")

        documents: list[DiscoveredDocument] = []
        newest: datetime | None = None
        seen_urls: set[str] = set()
        for raw in payload.get("articles", [])[:250]:
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
        published_at = cls._parse_seen_date(raw.get("seendate"))
        language_name = cls._bounded_text(raw.get("language"), 32)
        publisher = cls._bounded_text(raw.get("domain"), 300)
        summary = cls._bounded_text(raw.get("summary") or raw.get("description"), 20_000)
        metadata = {
            "gdelt_seen_date": published_at.isoformat() if published_at else None,
            "published_at_kind": "gdelt_seen_date",
            "source_country": cls._bounded_text(raw.get("sourcecountry"), 100),
            "social_image": cls._normalize_untrusted_url(raw.get("socialimage")),
            "original_language": language_name,
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
                language=_LANGUAGE_CODES.get((language_name or "").casefold(), "und"),
                published_at=published_at,
                metadata=metadata,
            )
        except ValidationError:
            return None

    @staticmethod
    def _bounded_text(value: object, maximum: int) -> str | None:
        if not isinstance(value, str):
            return None
        cleaned = " ".join(_CONTROL.sub(" ", value).split())
        return cleaned[:maximum] or None

    @staticmethod
    def _parse_seen_date(value: object) -> datetime | None:
        if not isinstance(value, str):
            return None
        try:
            return datetime.strptime(value, _GDELT_TIME).replace(tzinfo=UTC)
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
        hostname = parsed.hostname.rstrip(".").encode("idna").decode("ascii").casefold()
        if hostname == "localhost" or hostname.endswith(".local"):
            return None
        try:
            if ip_address(hostname).is_private:
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
    """Materialize bounded GDELT metadata without fetching the article body."""

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
        language=document.language or "und",
        metadata={
            **document.metadata,
            "content_kind": "discovery_metadata",
            "summary_available": document.summary is not None,
        },
    )
    return RawDocument(
        external=content,
        control=RawDocumentControl(
            document_id=f"gdelt-{url_digest[:32]}",
            source_id=document.source_id,
            state=DocumentState.DISCOVERED,
            state_version=0,
            content_sha256=url_digest,
            idempotency=IdempotencyKey(
                scope="gdelt-discovery",
                key=url_digest,
                payload_sha256=url_digest,
            ),
            discovered_at=document.discovered_at,
        ),
    )
