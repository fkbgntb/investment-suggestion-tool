"""Run one bounded Alpha Vantage news window and persist article metadata."""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta

from app.collectors.factory import build_safe_http_client
from app.config import Settings
from app.services.alpha_vantage_collection import AlphaVantageCollectionService
from app.storage.database import Database
from app.storage.migrations import upgrade_database
from app.storage.paths import prepare_storage_paths


async def run() -> int:
    settings = Settings()
    if settings.alpha_vantage_api_key is None:
        print("Alpha Vantage collection not started: configure the local API key")
        return 1
    prepare_storage_paths(settings)
    upgrade_database(settings.database_url)
    database = Database(settings.database_url)
    until = datetime.now(UTC)
    since = until - timedelta(hours=3)
    try:
        async with build_safe_http_client(settings) as http_client:
            with database.session() as session:
                outcome = await AlphaVantageCollectionService(
                    session,
                    settings.portfolio_workspace_id,
                    http_client,
                    settings.alpha_vantage_api_key,
                    max_records=settings.alpha_vantage_max_records,
                    max_calls_per_day=settings.alpha_vantage_max_calls_per_day,
                    max_documents_per_day=settings.alpha_vantage_max_documents_per_day,
                ).run("alpha-vantage-news", since=since, until=until)
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
