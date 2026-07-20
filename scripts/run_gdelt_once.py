"""Run one bounded GDELT discovery window and persist only article metadata."""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta

from app.collectors.factory import build_safe_http_client
from app.config import Settings
from app.services.gdelt_collection import GDELTCollectionService
from app.storage.database import Database
from app.storage.migrations import upgrade_database
from app.storage.paths import prepare_storage_paths


async def run() -> int:
    settings = Settings()
    prepare_storage_paths(settings)
    upgrade_database(settings.database_url)
    database = Database(settings.database_url)
    until = datetime.now(UTC)
    since = until - timedelta(hours=3)
    try:
        async with build_safe_http_client(settings) as http_client:
            with database.session() as session:
                outcome = await GDELTCollectionService(
                    session,
                    settings.portfolio_workspace_id,
                    http_client,
                    max_records=settings.gdelt_max_records,
                    max_documents_per_day=settings.gdelt_max_documents_per_day,
                ).run("gdelt-global-news", since=since, until=until)
    finally:
        database.dispose()
    print(f"crawl run: {outcome.crawl_run_id}")
    print(f"status: {outcome.status}")
    print(f"discovered: {outcome.discovered_count}")
    print(f"new: {outcome.created_count}")
    print(f"duplicates: {outcome.duplicate_count}")
    if outcome.error_code:
        print(f"error code: {outcome.error_code}")
    return 0 if outcome.status == "SUCCEEDED" else 1


def main() -> int:
    return asyncio.run(run())


if __name__ == "__main__":
    raise SystemExit(main())
