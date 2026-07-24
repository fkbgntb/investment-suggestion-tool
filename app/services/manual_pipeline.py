"""One bounded manual crawl-and-process run for the local web UI."""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass
from datetime import datetime, timedelta
from hashlib import sha256
from pathlib import Path
from uuid import NAMESPACE_URL, uuid5

from app.collectors.factory import build_safe_http_client
from app.collectors.registry import build_default_adapter_registry
from app.collectors.sec import SECCompany
from app.config import Settings
from app.services.alpha_vantage_collection import AlphaVantageCollectionService
from app.services.evidence_extraction import EvidenceExtractionService, build_evidence_provider
from app.services.evidence_scoring import EvidenceScoringService
from app.services.gdelt_collection import GDELTCollectionService
from app.services.normalization import NormalizationService
from app.services.official_document_collection import OfficialDocumentCollectionService
from app.services.relevance import RelevanceService
from app.services.report_triggers import ScheduledReportTriggerService
from app.services.sec_collection import SECCollectionService
from app.services.sources import SourceService
from app.storage.database import Database
from app.storage.repositories import TaskQueueRepository


@dataclass(frozen=True)
class ManualPipelineOutcome:
    source_count: int
    failed_source_count: int
    new_document_count: int
    normalized_count: int
    duplicate_count: int
    quarantined_count: int
    relevant_count: int
    review_count: int
    irrelevant_count: int
    extraction_count: int
    extraction_review_count: int
    scored_count: int
    report_generated_count: int = 0
    report_skipped_count: int = 0
    report_failed_count: int = 0
    report_outcome: str | None = None
    report_reason: str | None = None

    def as_dict(self) -> dict[str, object]:
        return asdict(self)


def _sec_companies() -> tuple[SECCompany, ...]:
    path = Path(__file__).resolve().parents[2] / "config_data" / "sources" / "sec-companies.json"
    return tuple(SECCompany.model_validate(item) for item in json.loads(path.read_text("utf-8")))


async def run_manual_pipeline(
    database: Database,
    settings: Settings,
    *,
    now: datetime,
) -> ManualPipelineOutcome:
    since = now - timedelta(hours=3)
    with database.session() as session:
        sources = SourceService(
            session,
            settings.portfolio_workspace_id,
            build_default_adapter_registry(),
        ).list_schedulable()
    created = failed = 0
    async with build_safe_http_client(settings) as client:
        for source in sources:
            try:
                with database.session() as session:
                    if source.adapter_name == "alpha-vantage-news":
                        if settings.alpha_vantage_api_key is None:
                            failed += 1
                            continue
                        result = await AlphaVantageCollectionService(
                            session,
                            settings.portfolio_workspace_id,
                            client,
                            settings.alpha_vantage_api_key,
                            max_records=settings.alpha_vantage_max_records,
                            max_calls_per_day=settings.alpha_vantage_max_calls_per_day,
                            max_documents_per_day=(settings.alpha_vantage_max_documents_per_day),
                        ).run(source.source_id, since=since, until=now)
                    elif source.adapter_name == "gdelt-doc":
                        result = await GDELTCollectionService(
                            session,
                            settings.portfolio_workspace_id,
                            client,
                            max_records=settings.gdelt_max_records,
                            max_documents_per_day=settings.gdelt_max_documents_per_day,
                        ).run(source.source_id, since=since, until=now)
                    elif source.adapter_name == "sec-submissions":
                        if settings.sec_contact_email is None:
                            failed += 1
                            continue
                        result = await SECCollectionService(
                            session,
                            settings.portfolio_workspace_id,
                            client,
                            _sec_companies(),
                            contact_email=settings.sec_contact_email,
                            max_filings_per_company=settings.sec_max_filings_per_company,
                        ).run(source.source_id, since=since, until=now)
                    elif source.adapter_name == "official-document":
                        result = await OfficialDocumentCollectionService(
                            session,
                            settings.portfolio_workspace_id,
                            client,
                        ).run(source.source_id, since=since, until=now)
                    else:
                        failed += 1
                        continue
                created += result.created_count
                failed += int(result.status != "SUCCEEDED")
            except Exception:
                failed += 1
    with database.session() as session:
        normalization = NormalizationService(
            session, settings.portfolio_workspace_id
        ).process_pending(now=now)
        try:
            relevance = RelevanceService(session, settings.portfolio_workspace_id).classify_pending(
                now=now
            )
        except RuntimeError:
            relevance = (0, 0, 0)
        provider = build_evidence_provider(settings)
        extraction = await EvidenceExtractionService(
            session,
            settings.portfolio_workspace_id,
            provider,
            model_version=(
                settings.deepseek_model if settings.deepseek_api_key is not None else "rules-1.0.0"
            ),
            max_input_characters=settings.deepseek_max_input_characters,
            max_calls_per_day=settings.deepseek_max_calls_per_day,
            daily_token_budget=settings.deepseek_daily_token_budget,
        ).extract_pending(now=now)
        scoring = EvidenceScoringService(session, settings.portfolio_workspace_id).score_pending(
            now=now
        )
        trigger_payload = {
            "manual_run_at": now.isoformat(),
            "new_document_count": created,
            "scored_count": scoring[0],
        }
        trigger_digest = sha256(
            json.dumps(trigger_payload, sort_keys=True, separators=(",", ":")).encode()
        ).hexdigest()
        TaskQueueRepository(session, settings.portfolio_workspace_id).enqueue(
            scope="manual:process-new-documents",
            key=trigger_digest,
            payload_sha256=trigger_digest,
            task_id=str(
                uuid5(
                    NAMESPACE_URL,
                    f"{settings.portfolio_workspace_id}:manual-report:{trigger_digest}",
                )
            ),
            task_type="process-new-documents",
            payload=trigger_payload,
            not_before=now,
        )
        report_batch = await ScheduledReportTriggerService(
            session,
            settings.portfolio_workspace_id,
            settings,
        ).consume_due(now=now)
        latest_outcome = report_batch.outcomes[-1] if report_batch.outcomes else None
    return ManualPipelineOutcome(
        source_count=len(sources),
        failed_source_count=failed,
        new_document_count=created,
        normalized_count=normalization[0],
        duplicate_count=normalization[1],
        quarantined_count=normalization[2],
        relevant_count=relevance[0],
        review_count=relevance[1],
        irrelevant_count=relevance[2],
        extraction_count=extraction[0],
        extraction_review_count=extraction[1],
        scored_count=scoring[0],
        report_generated_count=report_batch.generated,
        report_skipped_count=report_batch.skipped,
        report_failed_count=report_batch.failed,
        report_outcome=latest_outcome.status.value if latest_outcome is not None else None,
        report_reason=latest_outcome.reason if latest_outcome is not None else None,
    )
