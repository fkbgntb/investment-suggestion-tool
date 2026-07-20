"""Generate immutable HTML reports for pending validated analyses once."""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime

from app.config import Settings
from app.services.reports import ReportService
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
            generated, stale = await ReportService(
                session, settings.portfolio_workspace_id
            ).generate_pending(now=datetime.now(UTC))
    finally:
        database.dispose()
    print(f"generated: {generated}")
    print(f"stale: {stale}")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(run()))
