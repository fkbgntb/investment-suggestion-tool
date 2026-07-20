"""Run one SEC metadata collection window after local contact configuration."""

from __future__ import annotations

import asyncio
import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

from app.collectors.factory import build_safe_http_client
from app.collectors.sec import SECCompany
from app.config import Settings
from app.services.sec_collection import SECCollectionService
from app.storage.database import Database
from app.storage.migrations import upgrade_database
from app.storage.paths import prepare_storage_paths


def load_companies() -> tuple[SECCompany, ...]:
    path = Path(__file__).resolve().parents[1] / "config_data" / "sources" / "sec-companies.json"
    payload = json.loads(path.read_text(encoding="utf-8"))
    return tuple(SECCompany.model_validate(item) for item in payload)


async def run() -> int:
    settings = Settings()
    if settings.sec_contact_email is None:
        print("SEC collection not started: set INVEST_SEC_CONTACT_EMAIL in the local .env file")
        return 2
    prepare_storage_paths(settings)
    upgrade_database(settings.database_url)
    database = Database(settings.database_url)
    until = datetime.now(UTC)
    since = until - timedelta(hours=3)
    try:
        async with build_safe_http_client(settings) as http_client:
            with database.session() as session:
                outcome = await SECCollectionService(
                    session,
                    settings.portfolio_workspace_id,
                    http_client,
                    load_companies(),
                    contact_email=settings.sec_contact_email,
                    max_filings_per_company=settings.sec_max_filings_per_company,
                ).run("sec-company-filings", since=since, until=until)
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
