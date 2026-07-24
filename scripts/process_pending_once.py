"""Process saved documents and consume report tasks without fetching any source."""

from __future__ import annotations

import asyncio
import json
from datetime import UTC, datetime
from hashlib import sha256
from uuid import NAMESPACE_URL, uuid5

from app.config import Settings
from app.services.evidence_extraction import EvidenceExtractionService, build_evidence_provider
from app.services.evidence_scoring import EvidenceScoringService
from app.services.normalization import NormalizationService
from app.services.relevance import RelevanceService
from app.services.report_triggers import ScheduledReportTriggerService
from app.storage.database import Database
from app.storage.migrations import upgrade_database
from app.storage.paths import prepare_storage_paths
from app.storage.repositories import TaskQueueRepository


async def run() -> int:
    settings = Settings()
    prepare_storage_paths(settings)
    upgrade_database(settings.database_url)
    database = Database(settings.database_url)
    now = datetime.now(UTC)
    try:
        with database.session() as session:
            normalized = NormalizationService(
                session,
                settings.portfolio_workspace_id,
            ).process_pending(now=now)
            relevance = RelevanceService(
                session,
                settings.portfolio_workspace_id,
            ).classify_pending(now=now)
            extraction = await EvidenceExtractionService(
                session,
                settings.portfolio_workspace_id,
                build_evidence_provider(settings),
                model_version=(
                    settings.deepseek_model
                    if settings.deepseek_api_key is not None
                    else "rules-1.0.0"
                ),
                max_input_characters=settings.deepseek_max_input_characters,
                max_calls_per_day=settings.deepseek_max_calls_per_day,
                daily_token_budget=settings.deepseek_daily_token_budget,
            ).extract_pending(now=now, limit=6)
            scoring = EvidenceScoringService(
                session,
                settings.portfolio_workspace_id,
            ).score_pending(now=now)
            payload = {
                "processed_at": now.isoformat(),
                "normalized_count": normalized[0],
                "scored_count": scoring[0],
            }
            digest = sha256(
                json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
            ).hexdigest()
            TaskQueueRepository(session, settings.portfolio_workspace_id).enqueue(
                scope="local:process-pending",
                key=digest,
                payload_sha256=digest,
                task_id=str(
                    uuid5(
                        NAMESPACE_URL,
                        f"{settings.portfolio_workspace_id}:process-pending:{digest}",
                    )
                ),
                task_type="process-new-documents",
                payload=payload,
                not_before=now,
            )
            reports = await ScheduledReportTriggerService(
                session,
                settings.portfolio_workspace_id,
                settings,
            ).consume_due(now=now)
    finally:
        database.dispose()
    print(f"normalized={normalized[0]}; duplicates={normalized[1]}; quarantined={normalized[2]}")
    print(f"relevant={relevance[0]}; review={relevance[1]}; irrelevant={relevance[2]}")
    print(f"extracted={extraction[0]}; extraction_review={extraction[1]}")
    print(f"scored={scoring[0]}")
    print(
        f"reports_generated={reports.generated}; reports_skipped={reports.skipped}; "
        f"reports_failed={reports.failed}"
    )
    for outcome in reports.outcomes:
        print(f"report_outcome={outcome.status.value}; reason={outcome.reason}")
    return 0 if reports.failed == 0 else 1


def main() -> int:
    return asyncio.run(run())


if __name__ == "__main__":
    raise SystemExit(main())
