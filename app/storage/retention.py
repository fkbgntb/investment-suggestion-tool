"""Raw-content retention while preserving document metadata and hashes."""

from __future__ import annotations

from datetime import datetime, timedelta

from sqlalchemy import update
from sqlalchemy.orm import Session

from app.storage.models import RawDocumentRow


def purge_expired_raw_bodies(
    session: Session,
    *,
    workspace_id: str,
    now: datetime,
    retention_days: int,
) -> int:
    if retention_days < 1:
        raise ValueError("retention days must be positive")
    cutoff = now - timedelta(days=retention_days)
    statement = (
        update(RawDocumentRow)
        .where(
            RawDocumentRow.workspace_id == workspace_id,
            RawDocumentRow.raw_body.is_not(None),
            RawDocumentRow.fetched_at < cutoff,
        )
        .values(raw_body=None, body_purged_at=now, updated_at=now)
    )
    result = session.execute(statement)
    return int(result.rowcount or 0)
