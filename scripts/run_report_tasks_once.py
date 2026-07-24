"""Consume due local report tasks without fetching any external source."""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime

from app.config import Settings
from app.services.report_triggers import ScheduledReportTriggerService
from app.storage.database import Database
from app.storage.migrations import upgrade_database
from app.storage.paths import prepare_storage_paths


async def run() -> int:
    settings = Settings()
    prepare_storage_paths(settings)
    upgrade_database(settings.database_url)
    database = Database(settings.database_url)
    try:
        with database.session() as session:
            result = await ScheduledReportTriggerService(
                session,
                settings.portfolio_workspace_id,
                settings,
            ).consume_due(now=datetime.now(UTC))
    finally:
        database.dispose()
    print(f"reports generated: {result.generated}")
    print(f"report tasks skipped: {result.skipped}")
    print(f"report tasks failed: {result.failed}")
    return int(result.failed > 0)


def main() -> int:
    return asyncio.run(run())


if __name__ == "__main__":
    raise SystemExit(main())
