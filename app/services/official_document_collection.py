"""Durable collection of code-approved official documents."""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass
from datetime import datetime
from hashlib import sha256
from pathlib import Path
from uuid import NAMESPACE_URL, uuid5

from pydantic import TypeAdapter
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.collectors.official_documents import (
    OfficialDocumentAdapter,
    OfficialDocumentRejected,
    OfficialDocumentSpec,
)
from app.collectors.registry import AdapterRegistry
from app.collectors.safe_http import SafeFetchError, SafeHTTPClient
from app.domain.collection import FetchFailure, SourceHealthSnapshot
from app.domain.enums import FetchErrorCode, SourceHealthStatus
from app.services.market_metrics import extract_h30184_factsheet_snapshot
from app.services.sources import SourceConflict, SourceService
from app.storage.models import AssetRow
from app.storage.repositories import (
    CrawlRunInput,
    CrawlRunRepository,
    MarketSnapshotRepository,
    RawDocumentRepository,
)

_SPECS = TypeAdapter(tuple[OfficialDocumentSpec, ...])


def load_official_document_specs() -> tuple[OfficialDocumentSpec, ...]:
    path = (
        Path(__file__).resolve().parents[2] / "config_data" / "sources" / "official-documents.json"
    )
    return _SPECS.validate_python(json.loads(path.read_text(encoding="utf-8")))


@dataclass(frozen=True)
class OfficialCollectionOutcome:
    crawl_run_id: str
    status: str
    discovered_count: int = 0
    created_count: int = 0
    duplicate_count: int = 0
    error_code: str | None = None


class OfficialDocumentCollectionService:
    def __init__(
        self,
        session: Session,
        workspace_id: str,
        http_client: SafeHTTPClient,
        specs: tuple[OfficialDocumentSpec, ...] | None = None,
    ) -> None:
        self.session = session
        self.workspace_id = workspace_id
        self.http_client = http_client
        self.specs = specs if specs is not None else load_official_document_specs()
        self.sources = SourceService(
            session,
            workspace_id,
            AdapterRegistry(("official-document",)),
        )
        self.documents = RawDocumentRepository(session, workspace_id)
        self.runs = CrawlRunRepository(session)

    async def run(
        self,
        source_id: str,
        *,
        since: datetime,
        until: datetime,
    ) -> OfficialCollectionOutcome:
        source = self.sources.get(source_id)
        if source.adapter_name != "official-document":
            raise SourceConflict("source is not configured for official documents")
        selected = tuple(spec for spec in self.specs if spec.source_id == source_id)
        if not selected:
            raise SourceConflict("official source has no code-approved document specification")

        request_digest = sha256(
            f"{source_id}:{since.isoformat()}:{until.isoformat()}".encode()
        ).hexdigest()
        crawl_run_id = str(uuid5(NAMESPACE_URL, f"official:{self.workspace_id}:{request_digest}"))
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
                        "document_count": len(selected),
                        "since": since.isoformat(),
                        "until": until.isoformat(),
                    }
                },
            )
        )
        if not created:
            saved = row.payload.get("result", {})
            return OfficialCollectionOutcome(
                crawl_run_id=crawl_run_id,
                status=row.status,
                discovered_count=int(saved.get("discovered_count", 0)),
                created_count=int(saved.get("created_count", 0)),
                duplicate_count=int(saved.get("duplicate_count", 0)),
                error_code=(row.payload.get("failure") or {}).get("error_code"),
            )

        adapter = OfficialDocumentAdapter(self.http_client)
        created_count = 0
        try:
            for spec in selected:
                document = await adapter.fetch(
                    spec,
                    allowed_domains=source.allowed_domains,
                    fetched_at=until,
                )
                _, was_created = self.documents.add_if_absent(document)
                created_count += int(was_created)
                if spec.source_id == "csi-h30184-factsheet":
                    asset_exists = self.session.scalar(
                        select(AssetRow.asset_id).where(
                            AssetRow.workspace_id == self.workspace_id,
                            AssetRow.asset_id == "asset-007300",
                        )
                    )
                    snapshot = extract_h30184_factsheet_snapshot(document.external.body)
                    if asset_exists is not None and snapshot is not None:
                        MarketSnapshotRepository(
                            self.session,
                            self.workspace_id,
                        ).add_if_absent(snapshot)
        except SafeFetchError as error:
            return self._finish_failure(
                crawl_run_id,
                source_id,
                error.as_failure(until),
            )
        except OfficialDocumentRejected:
            return self._finish_failure(
                crawl_run_id,
                source_id,
                FetchFailure(
                    source_id=source_id,
                    error_code=FetchErrorCode.INVALID_RESPONSE,
                    retryable=False,
                    occurred_at=until,
                ),
            )

        outcome = OfficialCollectionOutcome(
            crawl_run_id=crawl_run_id,
            status="SUCCEEDED",
            discovered_count=len(selected),
            created_count=created_count,
            duplicate_count=len(selected) - created_count,
        )
        self.runs.mark_succeeded(
            workspace_id=self.workspace_id,
            crawl_run_id=crawl_run_id,
            finished_at=until,
            summary=asdict(outcome),
        )
        self.sources.record_health(self.http_client.health(source_id))
        return outcome

    def _finish_failure(
        self,
        crawl_run_id: str,
        source_id: str,
        failure: FetchFailure,
    ) -> OfficialCollectionOutcome:
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
                last_error_code=failure.error_code,
                last_success_at=previous.last_success_at,
                last_failure_at=failure.occurred_at,
            )
        )
        return OfficialCollectionOutcome(
            crawl_run_id=crawl_run_id,
            status="RETRYABLE_FAILED" if failure.retryable else "PERMANENT_FAILED",
            error_code=failure.error_code.value,
        )
