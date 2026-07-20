"""Normalize and deduplicate all pending documents once."""

from __future__ import annotations

from datetime import UTC, datetime

from app.config import Settings
from app.services.normalization import NormalizationService
from app.storage.database import Database
from app.storage.migrations import upgrade_database
from app.storage.paths import prepare_storage_paths


def main() -> int:
    settings = Settings()
    prepare_storage_paths(settings)
    upgrade_database(settings.database_url)
    database = Database(settings.database_url)
    try:
        with database.session() as session:
            processed, duplicates, quarantined = NormalizationService(
                session, settings.portfolio_workspace_id
            ).process_pending(now=datetime.now(UTC))
    finally:
        database.dispose()
    print(f"normalized documents: {processed}")
    print(f"exact duplicates: {duplicates}")
    print(f"quarantined documents: {quarantined}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
