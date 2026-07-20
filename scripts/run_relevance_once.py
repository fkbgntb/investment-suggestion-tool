"""Apply deterministic relevance screening to pending normalized documents."""

from __future__ import annotations

from datetime import UTC, datetime

from app.config import Settings
from app.services.relevance import RelevanceService
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
            relevant, review, irrelevant = RelevanceService(
                session, settings.portfolio_workspace_id
            ).classify_pending(now=datetime.now(UTC))
    finally:
        database.dispose()
    print(f"relevant documents: {relevant}")
    print(f"review documents: {review}")
    print(f"irrelevant documents: {irrelevant}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
