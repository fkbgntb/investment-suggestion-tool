"""Score all pending extracted evidence once."""

from __future__ import annotations

from datetime import UTC, datetime

from app.config import Settings
from app.services.evidence_scoring import EvidenceScoringService
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
            scored, positive, negative = EvidenceScoringService(
                session, settings.portfolio_workspace_id
            ).score_pending(now=datetime.now(UTC))
    finally:
        database.dispose()
    print(f"evidence scores created: {scored}")
    print(f"positive evidence: {positive}")
    print(f"negative evidence: {negative}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
