"""Run all due collection windows once; Windows Task Scheduler invokes this script."""

from __future__ import annotations

import argparse
import asyncio
import json
from datetime import UTC, datetime
from pathlib import Path

from app.collectors.factory import build_safe_http_client
from app.collectors.registry import build_default_adapter_registry
from app.collectors.sec import SECCompany
from app.config import Settings
from app.services.evidence_extraction import EvidenceExtractionService, build_evidence_provider
from app.services.evidence_scoring import EvidenceScoringService
from app.services.gdelt_collection import GDELTCollectionService
from app.services.normalization import NormalizationService
from app.services.relevance import RelevanceService
from app.services.scheduler import DurableJobScheduler, WindowCollectionResult
from app.services.sec_collection import SECCollectionService
from app.services.sources import SourceService
from app.storage.database import Database
from app.storage.migrations import upgrade_database
from app.storage.paths import prepare_storage_paths
from app.storage.repositories import AuditRepository, TaskQueueRepository
from app.storage.retention import purge_expired_raw_bodies


def load_sec_companies() -> tuple[SECCompany, ...]:
    path = Path(__file__).resolve().parents[1] / "config_data" / "sources" / "sec-companies.json"
    return tuple(SECCompany.model_validate(item) for item in json.loads(path.read_text("utf-8")))


async def run(*, force: bool = False) -> int:
    settings = Settings()
    prepare_storage_paths(settings)
    upgrade_database(settings.database_url)
    database = Database(settings.database_url)
    try:
        async with build_safe_http_client(settings) as http_client:

            async def collect_window(since: datetime, until: datetime) -> WindowCollectionResult:
                with database.session() as session:
                    sources = SourceService(
                        session,
                        settings.portfolio_workspace_id,
                        build_default_adapter_registry(),
                    ).list_schedulable()
                created_count = 0
                failed_count = 0
                for source in sources:
                    try:
                        with database.session() as session:
                            if source.adapter_name == "gdelt-doc":
                                outcome = await GDELTCollectionService(
                                    session,
                                    settings.portfolio_workspace_id,
                                    http_client,
                                    max_records=settings.gdelt_max_records,
                                    max_documents_per_day=settings.gdelt_max_documents_per_day,
                                ).run(source.source_id, since=since, until=until)
                            elif source.adapter_name == "sec-submissions":
                                if settings.sec_contact_email is None:
                                    failed_count += 1
                                    continue
                                outcome = await SECCollectionService(
                                    session,
                                    settings.portfolio_workspace_id,
                                    http_client,
                                    load_sec_companies(),
                                    contact_email=settings.sec_contact_email,
                                    max_filings_per_company=(settings.sec_max_filings_per_company),
                                ).run(source.source_id, since=since, until=until)
                            else:
                                failed_count += 1
                                continue
                        created_count += outcome.created_count
                        failed_count += int(outcome.status != "SUCCEEDED")
                    except Exception as error:
                        failed_count += 1
                        print(f"source run failed: {source.source_id} ({type(error).__name__})")
                return WindowCollectionResult(
                    created_count=created_count,
                    source_count=len(sources),
                    failed_source_count=failed_count,
                )

            def cleanup(now: datetime) -> int:
                with database.session() as session:
                    count = purge_expired_raw_bodies(
                        session,
                        workspace_id=settings.portfolio_workspace_id,
                        now=now,
                        retention_days=settings.raw_document_retention_days,
                    )
                    AuditRepository(session, settings.portfolio_workspace_id).record(
                        event_type="scheduled_retention",
                        actor="local_scheduler",
                        target_type="workspace",
                        target_id=settings.portfolio_workspace_id,
                        outcome="completed",
                        details={"purged_document_count": count},
                    )
                    return count

            outcome = await DurableJobScheduler(
                database,
                settings.portfolio_workspace_id,
            ).run_due(now=datetime.now(UTC), runner=collect_window, cleanup=cleanup, force=force)

        normalization_counts = (0, 0, 0)
        relevance_counts = (0, 0, 0)
        extraction_counts = (0, 0, 0)
        scoring_counts = (0, 0, 0)
        normalized_at = datetime.now(UTC)
        with database.session() as session:
            tasks = TaskQueueRepository(session, settings.portfolio_workspace_id).list_due(
                "process-new-documents", now=normalized_at
            )
            if tasks:
                normalizer = NormalizationService(session, settings.portfolio_workspace_id)
                totals = [0, 0, 0]
                while True:
                    batch = normalizer.process_pending(now=normalized_at)
                    totals = [left + right for left, right in zip(totals, batch, strict=True)]
                    if batch[0] + batch[2] < 500:
                        break
                normalization_counts = tuple(totals)
                task_repository = TaskQueueRepository(session, settings.portfolio_workspace_id)
                for task in tasks:
                    task_repository.mark_succeeded(task.task_id, finished_at=normalized_at)
            try:
                relevance_counts = RelevanceService(
                    session, settings.portfolio_workspace_id
                ).classify_pending(now=normalized_at)
            except RuntimeError as error:
                print(f"relevance screening skipped: {error}")
            provider = build_evidence_provider(settings)
            extraction_counts = await EvidenceExtractionService(
                session,
                settings.portfolio_workspace_id,
                provider,
                model_version=(
                    settings.deepseek_model
                    if settings.deepseek_api_key is not None
                    else "rules-1.0.0"
                ),
                max_input_characters=settings.deepseek_max_input_characters,
                max_calls_per_day=settings.deepseek_max_calls_per_day,
                daily_token_budget=settings.deepseek_daily_token_budget,
            ).extract_pending(now=normalized_at)
            scoring_counts = EvidenceScoringService(
                session, settings.portfolio_workspace_id
            ).score_pending(now=normalized_at)
    finally:
        database.dispose()
    print(f"scheduler status: {outcome.status}")
    print(f"windows: {outcome.window_count}")
    print(f"new documents: {outcome.created_count}")
    print(f"failed sources: {outcome.failed_source_count}")
    print(f"processing tasks: {outcome.processing_tasks}")
    print(f"normalized documents: {normalization_counts[0]}")
    print(f"exact duplicates: {normalization_counts[1]}")
    print(f"quarantined documents: {normalization_counts[2]}")
    print(f"relevant documents: {relevance_counts[0]}")
    print(f"relevance review documents: {relevance_counts[1]}")
    print(f"irrelevant documents: {relevance_counts[2]}")
    print(f"evidence extractions succeeded: {extraction_counts[0]}")
    print(f"evidence extractions need review: {extraction_counts[1]}")
    print(f"AI daily budget reached: {bool(extraction_counts[2])}")
    print(f"evidence scores created: {scoring_counts[0]}")
    print(f"positive evidence: {scoring_counts[1]}")
    print(f"negative evidence: {scoring_counts[2]}")
    return 0 if outcome.status in {"SUCCEEDED", "NOT_DUE", "LOCKED"} else 1


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--force",
        action="store_true",
        help="run a safe due-window check now without changing the job function",
    )
    return asyncio.run(run(force=parser.parse_args().force))


if __name__ == "__main__":
    raise SystemExit(main())
