"""Extract deterministic market observations from already-saved official documents."""

from __future__ import annotations

from sqlalchemy import select

from app.config import Settings
from app.services.market_metrics import extract_h30184_factsheet_snapshot
from app.storage.database import Database
from app.storage.models import RawDocumentRow
from app.storage.repositories import MarketSnapshotRepository


def main() -> int:
    settings = Settings()
    database = Database(settings.database_url)
    created = 0
    try:
        with database.session() as session:
            rows = session.scalars(
                select(RawDocumentRow)
                .where(
                    RawDocumentRow.workspace_id == settings.portfolio_workspace_id,
                    RawDocumentRow.source_id == "csi-h30184-factsheet",
                )
                .order_by(RawDocumentRow.fetched_at.desc())
            ).all()
            repository = MarketSnapshotRepository(
                session,
                settings.portfolio_workspace_id,
            )
            for row in rows:
                snapshot = extract_h30184_factsheet_snapshot(row.raw_body or "")
                if snapshot is None:
                    continue
                _, was_created = repository.add_if_absent(snapshot)
                created += int(was_created)
    finally:
        database.dispose()
    print(f"market_snapshots_created={created}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
