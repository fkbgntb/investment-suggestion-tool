"""One bounded SEC submissions run with durable summary, health, and cursor."""

from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import datetime
from hashlib import sha256
from uuid import NAMESPACE_URL, uuid5

from sqlalchemy.orm import Session

from app.collectors.registry import AdapterRegistry
from app.collectors.safe_http import SafeFetchError, SafeHTTPClient
from app.collectors.sec import (
    SECAdapter,
    SECCompany,
    SECResponseError,
    sec_discovery_to_raw_document,
)
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
class SECCollectionOutcome:
    crawl_run_id: str
    status: str
    discovered_count: int = 0
    created_count: int = 0
    duplicate_count: int = 0
    next_cursor: str | None = None
    error_code: str | None = None


class SECCollectionService:
    def __init__(
        self,
        session: Session,
        workspace_id: str,
        http_client: SafeHTTPClient,
        companies: tuple[SECCompany, ...],
        *,
        contact_email: str,
        max_filings_per_company: int = 50,
    ) -> None:
        self.session = session
        self.workspace_id = workspace_id
        self.http_client = http_client
        self.companies = companies
        self.contact_email = contact_email
        self.max_filings_per_company = max_filings_per_company
        self.sources = SourceService(
            session,
            workspace_id,
            AdapterRegistry(("sec-submissions",)),
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
    ) -> SECCollectionOutcome:
        source = self.sources.get(source_id)
        if source.adapter_name != "sec-submissions":
            raise SourceConflict("source is not configured for the SEC adapter")
        if not source.enabled:
            raise SourceConflict("disabled sources cannot be collected")
        configuration = self.taxonomy.get_active()
        if configuration is None:
            raise SourceConflict("an active taxonomy configuration is required")
        entities = {entity.entity_id for entity in configuration.entities if entity.enabled}
        if any(company.entity_id not in entities for company in self.companies):
            raise SourceConflict("SEC company configuration references an inactive entity")
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
        digest = sha256(request.model_dump_json(exclude_none=True).encode()).hexdigest()
        crawl_run_id = str(uuid5(NAMESPACE_URL, f"sec:{self.workspace_id}:{digest}"))
        row, created = self.runs.add_if_absent(
            CrawlRunInput(
                crawl_run_id=crawl_run_id,
                workspace_id=self.workspace_id,
                source_id=source_id,
                idempotency_key=digest,
                status="RUNNING",
                scheduled_at=until,
                payload={
                    "request": {
                        "company_count": len(self.companies),
                        "form_count": len({form for item in self.companies for form in item.forms}),
                        "since": since.isoformat(),
                        "until": until.isoformat(),
                        "cursor_supplied": state is not None and state.cursor is not None,
                    }
                },
            )
        )
        if not created:
            result = row.payload.get("result", {})
            return SECCollectionOutcome(
                crawl_run_id=crawl_run_id,
                status=row.status,
                discovered_count=int(result.get("discovered_count", 0)),
                created_count=int(result.get("created_count", 0)),
                duplicate_count=int(result.get("duplicate_count", 0)),
                next_cursor=result.get("next_cursor"),
                error_code=(row.payload.get("failure") or {}).get("error_code"),
            )

        adapter = SECAdapter(
            self.http_client,
            self.companies,
            contact_email=self.contact_email,
            max_filings_per_company=self.max_filings_per_company,
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
            return SECCollectionOutcome(
                crawl_run_id=crawl_run_id,
                status="RETRYABLE_FAILED" if error.retryable else "PERMANENT_FAILED",
                error_code=error.error_code.value,
            )
        except SECResponseError:
            return self._invalid_response(crawl_run_id, source_id, until)

        created_count = 0
        for discovered in result.documents:
            _, was_created = self.documents.add_if_absent(sec_discovery_to_raw_document(discovered))
            created_count += int(was_created)
        outcome = SECCollectionOutcome(
            crawl_run_id=crawl_run_id,
            status="SUCCEEDED",
            discovered_count=len(result.documents),
            created_count=created_count,
            duplicate_count=len(result.documents) - created_count,
            next_cursor=result.next_cursor,
        )
        self.runs.mark_succeeded(
            workspace_id=self.workspace_id,
            crawl_run_id=crawl_run_id,
            finished_at=until,
            summary=asdict(outcome),
        )
        self.sources.advance_cursor(
            source_id,
            adapter_version="sec-submissions-1.0",
            cursor=result.next_cursor,
            expected_version=state.state_version if state is not None else 0,
            occurred_at=until,
        )
        self.sources.record_health(self.http_client.health(source_id))
        return outcome

    def _invalid_response(
        self, crawl_run_id: str, source_id: str, occurred_at: datetime
    ) -> SECCollectionOutcome:
        failure = FetchFailure(
            source_id=source_id,
            error_code=FetchErrorCode.INVALID_RESPONSE,
            retryable=True,
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
                last_error_code=FetchErrorCode.INVALID_RESPONSE,
                last_success_at=previous.last_success_at,
                last_failure_at=occurred_at,
            )
        )
        return SECCollectionOutcome(
            crawl_run_id=crawl_run_id,
            status="RETRYABLE_FAILED",
            error_code=FetchErrorCode.INVALID_RESPONSE.value,
        )
