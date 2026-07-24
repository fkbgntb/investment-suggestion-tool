"""Run only the reviewed official sources without consuming a news API quota."""

from __future__ import annotations

import argparse
import asyncio
from datetime import UTC, datetime, timedelta

from app.collectors.factory import build_safe_http_client
from app.collectors.registry import build_default_adapter_registry
from app.config import Settings
from app.services.official_document_collection import OfficialDocumentCollectionService
from app.services.sources import SourceService
from app.storage.database import Database
from app.storage.migrations import upgrade_database
from app.storage.paths import prepare_storage_paths


async def run(source_id: str | None) -> int:
    settings = Settings()
    prepare_storage_paths(settings)
    upgrade_database(settings.database_url)
    database = Database(settings.database_url)
    now = datetime.now(UTC)
    try:
        with database.session() as session:
            sources = tuple(
                source
                for source in SourceService(
                    session,
                    settings.portfolio_workspace_id,
                    build_default_adapter_registry(),
                ).list_schedulable()
                if source.adapter_name == "official-document"
                and (source_id is None or source.source_id == source_id)
            )
        if source_id is not None and not sources:
            raise ValueError("the requested official source is not registered and enabled")
        failures = 0
        async with build_safe_http_client(settings) as client:
            for source in sources:
                with database.session() as session:
                    outcome = await OfficialDocumentCollectionService(
                        session,
                        settings.portfolio_workspace_id,
                        client,
                    ).run(
                        source.source_id,
                        since=now - timedelta(hours=source.crawl_interval_hours),
                        until=now,
                    )
                print(
                    f"{source.source_id}: {outcome.status}; "
                    f"new={outcome.created_count}; duplicate={outcome.duplicate_count}"
                )
                failures += int(outcome.status != "SUCCEEDED")
        return 0 if failures == 0 else 1
    finally:
        database.dispose()


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source-id")
    arguments = parser.parse_args()
    return asyncio.run(run(arguments.source_id))


if __name__ == "__main__":
    raise SystemExit(main())
