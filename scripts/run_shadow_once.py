"""Run one crawl + advisory analysis cycle and save a shadow-run record."""

from __future__ import annotations

import argparse
import asyncio
from datetime import UTC, datetime
from decimal import Decimal

from app.config import Settings
from app.services.manual_pipeline import run_manual_pipeline
from app.services.quality import (
    build_shadow_record,
    collect_quality_metrics,
    record_shadow_audit,
    save_shadow_record,
)
from app.services.reports import ReportService
from app.storage.database import Database
from app.storage.migrations import upgrade_database
from app.storage.paths import prepare_storage_paths
from app.storage.repositories import PortfolioRepository


async def run(position_id: str | None, portfolio_value: Decimal | None) -> int:
    settings = Settings()
    prepare_storage_paths(settings)
    upgrade_database(settings.database_url)
    reference_value = portfolio_value or settings.portfolio_reference_value
    if reference_value is None:
        raise ValueError("set INVEST_PORTFOLIO_REFERENCE_VALUE or pass --portfolio-value")
    effective_settings = settings.model_copy(update={"portfolio_reference_value": reference_value})
    database = Database(settings.database_url)
    started_at = datetime.now(UTC)
    try:
        with database.session() as session:
            positions = PortfolioRepository(
                session, settings.portfolio_workspace_id
            ).list_positions()
            if not positions:
                raise ValueError("no saved position is available")
            selected = position_id or positions[0].position_id
            previous_values = ReportService(session, settings.portfolio_workspace_id).list_reports(
                limit=1
            )
            previous_report = previous_values[0] if previous_values else None

        crawl = await run_manual_pipeline(database, effective_settings, now=datetime.now(UTC))
        with database.session() as session:
            reports = ReportService(session, settings.portfolio_workspace_id)
            if crawl.report_id is not None:
                saved = reports.get(crawl.report_id)
                if saved is None:
                    raise RuntimeError("the generated report could not be loaded")
                report, _ = saved
            else:
                latest = reports.list_reports(limit=1)
                if not latest:
                    raise RuntimeError(
                        "the pipeline did not generate a report and no prior report exists"
                    )
                report = latest[0]
            difference = None
            if previous_report is not None and previous_report.report_id != report.report_id:
                difference = reports.diff(previous_report.report_id, report.report_id)
            finished_at = datetime.now(UTC)
            metrics = collect_quality_metrics(
                session, settings.portfolio_workspace_id, now=finished_at
            )
            record = build_shadow_record(
                position_id=selected,
                report=report,
                previous_report=previous_report,
                difference=difference,
                crawl=crawl,
                metrics=metrics,
                started_at=started_at,
                finished_at=finished_at,
            )
            record_shadow_audit(session, settings.portfolio_workspace_id, record)
        output = save_shadow_record(record, settings.data_dir)
    finally:
        database.dispose()
    print(f"shadow run: {record.shadow_run_id}")
    print(f"status: {record.status}")
    print(f"decision: {record.decision_label.value}")
    print(f"record: {output}")
    print("advisory only: true")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--position-id")
    parser.add_argument("--portfolio-value", type=Decimal)
    arguments = parser.parse_args()
    return asyncio.run(run(arguments.position_id, arguments.portfolio_value))


if __name__ == "__main__":
    raise SystemExit(main())
