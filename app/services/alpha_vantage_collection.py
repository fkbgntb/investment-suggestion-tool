"""One bounded Alpha Vantage news collection run with durable state."""

from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from hashlib import sha256
from uuid import NAMESPACE_URL, uuid5

from pydantic import SecretStr
from sqlalchemy.orm import Session

from app.collectors.alpha_vantage import (
    AlphaVantageAdapter,
    AlphaVantageRateLimitReached,
    AlphaVantageResponseError,
    discovered_to_raw_document,
)
from app.collectors.registry import AdapterRegistry
from app.collectors.safe_http import SafeFetchError, SafeHTTPClient
from app.domain.collection import FetchFailure, SourceHealthSnapshot
from app.domain.contracts import SourceDiscoveryRequest
from app.domain.enums import FetchErrorCode, SourceHealthStatus
from app.services.sources import SourceConflict, SourceService
from app.storage.repositories import (
    CrawlRunInput,
    CrawlRunRepository,
    RawDocumentRepository,
    TaxonomyRepository,
)


@dataclass(frozen=True)
class AlphaVantageCollectionOutcome:
    crawl_run_id: str
    status: str
    discovered_count: int = 0
    created_count: int = 0
    duplicate_count: int = 0
    truncated: bool = False
    next_cursor: str | None = None
    error_code: str | None = None


class AlphaVantageCollectionService:
    def __init__(
        self,
        session: Session,
        workspace_id: str,
        http_client: SafeHTTPClient,
        api_key: SecretStr,
        *,
        max_records: int = 50,
        max_calls_per_day: int = 20,
        max_documents_per_day: int = 500,
    ) -> None:
        self.session = session
        self.workspace_id = workspace_id
        self.http_client = http_client
        self.api_key = api_key
        self.max_records = max_records
        self.max_calls_per_day = max_calls_per_day
        self.max_documents_per_day = max_documents_per_day
        self.sources = SourceService(
            session,
            workspace_id,
            AdapterRegistry(("alpha-vantage-news",)),
        )
        self.taxonomy = TaxonomyRepository(session, workspace_id)
        self.documents = RawDocumentRepository(session, workspace_id)
        self.runs = CrawlRunRepository(session)

    async def run(
        self,
        source_id: str,
        *,
        since: datetime,
        until: datetime,
    ) -> AlphaVantageCollectionOutcome:
        source = self.sources.get(source_id)
        if source.adapter_name != "alpha-vantage-news":
            raise SourceConflict("source is not configured for the Alpha Vantage adapter")
        if not source.enabled:
            raise SourceConflict("disabled sources cannot be collected")
        configuration = self.taxonomy.get_active()
        if configuration is None:
            raise SourceConflict("an active taxonomy configuration is required")
        topic_ids = tuple(topic.topic_id for topic in configuration.topics if topic.enabled)[:50]
        if not topic_ids:
            raise SourceConflict("the active taxonomy has no enabled topics")

        state = self.sources.adapter_state(source_id)
        request = SourceDiscoveryRequest(
            source_id=source_id,
            topic_ids=topic_ids,
            since=since,
            until=until,
            cursor=state.cursor if state is not None else None,
        )
        request_digest = sha256(
            request.model_dump_json(exclude_none=True).encode("utf-8")
        ).hexdigest()
        crawl_run_id = str(
            uuid5(NAMESPACE_URL, f"alpha-vantage:{self.workspace_id}:{request_digest}")
        )
        start_of_day = until.astimezone(UTC).replace(hour=0, minute=0, second=0, microsecond=0)
        used_calls = self.runs.count_since(
            workspace_id=self.workspace_id,
            source_id=source_id,
            since=start_of_day,
        )
        row, created = self.runs.add_if_absent(
            CrawlRunInput(
                crawl_run_id=crawl_run_id,
                workspace_id=self.workspace_id,
                source_id=source_id,
                idempotency_key=request_digest,
                status="RUNNING",
                scheduled_at=until,
                payload={
                    "request": {
                        "topic_count": len(request.topic_ids),
                        "since": request.since.isoformat(),
                        "until": request.until.isoformat(),
                        "cursor_supplied": request.cursor is not None,
                    }
                },
            )
        )
        if not created:
            saved = row.payload.get("result", {})
            return AlphaVantageCollectionOutcome(
                crawl_run_id=crawl_run_id,
                status=row.status,
                discovered_count=int(saved.get("discovered_count", 0)),
                created_count=int(saved.get("created_count", 0)),
                duplicate_count=int(saved.get("duplicate_count", 0)),
                truncated=bool(saved.get("truncated", False)),
                next_cursor=saved.get("next_cursor"),
                error_code=(row.payload.get("failure") or {}).get("error_code"),
            )
        if used_calls >= self.max_calls_per_day:
            return self._finish_failure(
                crawl_run_id,
                source_id,
                FetchErrorCode.DAILY_LIMIT_REACHED,
                retryable=False,
                occurred_at=until,
            )

        used_documents = self.documents.count_since(source_id=source_id, since=start_of_day)
        remaining_documents = self.max_documents_per_day - used_documents
        if remaining_documents <= 0:
            return self._finish_failure(
                crawl_run_id,
                source_id,
                FetchErrorCode.DAILY_LIMIT_REACHED,
                retryable=False,
                occurred_at=until,
            )

        adapter = AlphaVantageAdapter(
            self.http_client,
            self.api_key,
            max_records=min(self.max_records, remaining_documents),
        )
        try:
            result = await adapter.discover(request)
        except SafeFetchError as error:
            self.runs.mark_fetch_failure(
                workspace_id=self.workspace_id,
                crawl_run_id=crawl_run_id,
                failure=error.as_failure(until),
            )
            self.sources.record_health(self.http_client.health(source_id))
            return AlphaVantageCollectionOutcome(
                crawl_run_id=crawl_run_id,
                status="RETRYABLE_FAILED" if error.retryable else "PERMANENT_FAILED",
                error_code=error.error_code.value,
            )
        except AlphaVantageRateLimitReached:
            return self._finish_failure(
                crawl_run_id,
                source_id,
                FetchErrorCode.RATE_LIMITED,
                retryable=True,
                occurred_at=until,
            )
        except AlphaVantageResponseError:
            return self._finish_failure(
                crawl_run_id,
                source_id,
                FetchErrorCode.INVALID_RESPONSE,
                retryable=True,
                occurred_at=until,
            )

        created_count = 0
        for discovered in result.documents:
            _, was_created = self.documents.add_if_absent(discovered_to_raw_document(discovered))
            created_count += int(was_created)
        outcome = AlphaVantageCollectionOutcome(
            crawl_run_id=crawl_run_id,
            status="SUCCEEDED",
            discovered_count=len(result.documents),
            created_count=created_count,
            duplicate_count=len(result.documents) - created_count,
            truncated=adapter.last_truncated or len(result.documents) >= remaining_documents,
            next_cursor=result.next_cursor,
        )
        summary = {
            **asdict(outcome),
            "query_sha256": adapter.last_query_sha256,
            "focus_ticker": adapter.last_focus_ticker,
        }
        self.runs.mark_succeeded(
            workspace_id=self.workspace_id,
            crawl_run_id=crawl_run_id,
            finished_at=until,
            summary=summary,
        )
        expected_version = state.state_version if state is not None else 0
        self.sources.advance_cursor(
            source_id,
            adapter_version="alpha-vantage-news-1.0",
            cursor=result.next_cursor or request.cursor,
            expected_version=expected_version,
            occurred_at=until,
        )
        self.sources.record_health(self.http_client.health(source_id))
        return outcome

    def _finish_failure(
        self,
        crawl_run_id: str,
        source_id: str,
        error_code: FetchErrorCode,
        *,
        retryable: bool,
        occurred_at: datetime,
    ) -> AlphaVantageCollectionOutcome:
        failure = FetchFailure(
            source_id=source_id,
            error_code=error_code,
            retryable=retryable,
            occurred_at=occurred_at,
        )
        self.runs.mark_fetch_failure(
            workspace_id=self.workspace_id,
            crawl_run_id=crawl_run_id,
            failure=failure,
        )
        previous = self.sources.health(source_id)
        self.sources.record_health(
            SourceHealthSnapshot(
                source_id=source_id,
                status=SourceHealthStatus.DEGRADED,
                consecutive_failures=previous.consecutive_failures + 1,
                last_error_code=error_code,
                last_success_at=previous.last_success_at,
                last_failure_at=occurred_at,
            )
        )
        return AlphaVantageCollectionOutcome(
            crawl_run_id=crawl_run_id,
            status="RETRYABLE_FAILED" if retryable else "PERMANENT_FAILED",
            error_code=error_code.value,
        )
